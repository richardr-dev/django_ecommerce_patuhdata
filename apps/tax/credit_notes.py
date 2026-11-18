"""Nota kredit: correcting an invoice after it was issued.

Posting (``("credit_note", id)``): the net sale comes off Retur & refund penjualan, the PPN off
PPN keluaran, and the credit goes to Piutang usaha up to what the buyer still owed. What they had
already paid is refunded (bank/cash) or kept as their credit. Goods that come back return to the
shelf and to Persediaan at their cost. Invoices the ledger books through a gateway or marketplace
payment have no receivable, so the whole note is refunded or kept as credit."""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.accounting.ledger import (
    CASH,
    COGS,
    CUSTOMER_CREDIT,
    INVENTORY,
    MARKETPLACE,
    RECEIVABLE,
    REFUNDS,
    VAT,
    LedgerError,
    money,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from apps.orders.models import OrderItem
from apps.orders.stock import StockError, stock_in, stock_out

from .models import CreditNote, CreditNoteItem, CustomerCredit, Invoice
from .numbering import next_number
from .posting import booked_by_invoice, refresh_status, status_for, sync_invoice

logger = logging.getLogger(__name__)


class CreditNoteError(ValueError):
    pass


def credited_quantity(invoice_item):
    """Units of an invoice line already credited by notes that stand."""
    return invoice_item.credited_lines.exclude(note__status=CreditNote.STATUS_VOID).aggregate(
        total=Sum("quantity", default=0))["total"]


def creditable(invoice):
    """Lines of an invoice with what is left to credit on each."""
    rows = []
    for item in invoice.items.select_related("product"):
        left = item.quantity - credited_quantity(item)
        if left > 0:
            rows.append({"item": item, "left": left})
    return rows


def _unit_cost(invoice, item):
    """What the returned unit cost when it was sold: the order line's cost, else the product's."""
    line = None
    if item.product_id:
        line = OrderItem.objects.filter(order=invoice.order, product_id=item.product_id).first()
    cost = line.unit_cost if line and line.unit_cost is not None else None
    if cost is None and item.product_id:
        cost = item.product.cost_price
    return cost or Decimal(0), (line.variant if line else None)


def _post(note, *, strict=False, actor=None):
    invoice = note.invoice
    booked = booked_by_invoice(invoice)
    direct = note.paid_out if booked else note.total
    if note.excess_action == CreditNote.EXCESS_CREDIT:
        target = CUSTOMER_CREDIT
    elif note.excess_action == CreditNote.EXCESS_PLATFORM:
        target = MARKETPLACE  # the marketplace pays the buyer and takes it off the next payout
    else:
        target = note.bank_account.account.code if note.bank_account_id else CASH
    amounts = {REFUNDS: note.subtotal, VAT: note.tax, RECEIVABLE: -(note.total - direct)}
    amounts[target] = amounts.get(target, Decimal(0)) - direct
    cost = sum((item.restocked_cost * item.quantity for item in note.items.all() if item.restock),
        Decimal(0))
    if cost:
        amounts[INVENTORY] = cost
        amounts[COGS] = -cost
    return post_entry(note.store, date=note.issued_on, strict=strict, actor=actor,
        source=("credit_note", note.pk),
        memo=f"Nota kredit {note.number} · invoice {invoice.number}", amounts=amounts)


@transaction.atomic
def create_credit_note(invoice, *, lines, issued_on, reason="return", notes="",
        excess_action=CreditNote.EXCESS_CREDIT, bank_account=None, warehouse=None, actor=None):
    """``lines`` is ``[{"item": InvoiceItem or None, "description", "quantity", "unit_price",
    "restock": bool}]``. Raises CreditNoteError when the note is not allowed. Restocked units go to
    ``warehouse`` (the store's default when not given, e.g. a register refund names its own outlet)."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status in {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}:
        raise CreditNoteError("Invoice ini tidak bisa dibuatkan nota kredit.")
    if bank_account and bank_account.store_id != invoice.store_id:
        raise CreditNoteError("Rekening tidak ditemukan.")
    if excess_action == CreditNote.EXCESS_PLATFORM and \
            invoice.order.channel not in CreditNote.MARKETPLACE_CHANNELS:
        raise CreditNoteError("Potongan pencairan hanya untuk pesanan marketplace.")
    rows, subtotal = [], Decimal(0)
    for line in lines:
        quantity, price = Decimal(line["quantity"]), money(line["unit_price"])
        if quantity <= 0 or price < 0:
            continue
        item = line.get("item")
        if item is not None:
            if item.invoice_id != invoice.pk:
                raise CreditNoteError("Baris bukan milik invoice ini.")
            left = item.quantity - credited_quantity(item)
            if quantity > left:
                raise CreditNoteError(f"{item.description}: hanya {left:g} yang masih bisa "
                    "dikreditkan.")
        total = money(quantity * price)
        subtotal += total
        rows.append({**line, "quantity": quantity, "unit_price": price, "line_total": total})
    if not rows or subtotal <= 0:
        raise CreditNoteError("Isi minimal satu baris dengan jumlah lebih dari nol.")
    tax = money(subtotal * invoice.tax_rate / 100) if invoice.tax_rate else Decimal(0)
    total = subtotal + tax
    already = invoice.credit_notes.exclude(status=CreditNote.STATUS_VOID).aggregate(
        total=Sum("total", default=0))["total"]
    if already + total > invoice.total:
        raise CreditNoteError(f"Total nota kredit (Rp{already + total:,.0f}) melebihi nilai "
            f"invoice (Rp{invoice.total:,.0f}).".replace(",", "."))
    booked = booked_by_invoice(invoice)
    if booked:
        sync_invoice(invoice, actor=actor)  # an invoice older than ledger posting
        owed = invoice.amount_due
        applied = min(total, owed)
    else:
        applied = Decimal(0)
    paid_out = total - applied
    note = CreditNote.objects.create(store=invoice.store, invoice=invoice,
        number=next_number(invoice.store, "CN", issued_on), issued_on=issued_on, reason=reason,
        notes=notes[:500], buyer_name=invoice.buyer_name, buyer_npwp=invoice.buyer_npwp,
        subtotal=subtotal, tax_rate=invoice.tax_rate, tax=tax, total=total, applied=applied,
        paid_out=paid_out, excess_action=excess_action, bank_account=bank_account,
        created_by=actor)
    for row in rows:
        item = row.get("item")
        cost, variant = (_unit_cost(invoice, item) if item is not None else (Decimal(0), None))
        restock = bool(row.get("restock") and item is not None and item.product_id)
        CreditNoteItem.objects.create(note=note, invoice_item=item,
            product=item.product if item else None, variant=variant if restock else None,
            description=row.get("description") or (item.description if item else "Koreksi"),
            sku=item.sku if item else "", quantity=row["quantity"], unit_price=row["unit_price"],
            line_total=row["line_total"], restock=restock, restocked_cost=cost if restock else 0)
    try:
        _post(note, strict=True, actor=actor)
        for line in note.items.filter(restock=True).select_related("product", "variant"):
            stock_in(store=invoice.store, product=line.product, variant=line.variant,
                quantity=int(line.quantity), kind="return", actor=actor,
                reason=f"Retur dari nota kredit {note.number}", warehouse=warehouse,
                source=("credit_note", note.pk), unit_cost=line.restocked_cost)
    except (LedgerError, StockError) as error:
        raise CreditNoteError(str(error)) from error
    if paid_out and excess_action == CreditNote.EXCESS_CREDIT:
        CustomerCredit.objects.create(store=invoice.store, customer=invoice.order.customer,
            buyer_name=invoice.buyer_name, kind="credit_note", date=issued_on, amount=paid_out,
            note=f"Nota kredit {note.number}", invoice=invoice, credit_note=note, created_by=actor)
    refresh_status(invoice)
    return note


@transaction.atomic
def void_credit_note(note, *, actor=None):
    note = CreditNote.objects.select_for_update().get(pk=note.pk)
    if note.status == CreditNote.STATUS_VOID:
        return note
    try:
        for line in note.items.filter(restock=True).select_related("product", "variant"):
            stock_out(store=note.store, product=line.product, variant=line.variant,
                quantity=int(line.quantity), kind="adjust", actor=actor,
                reason=f"Pembatalan nota kredit {note.number}",
                source=("credit_note", note.pk), unit_cost=line.restocked_cost)
        reverse_source(note.store, "credit_note", note.pk, actor=actor, strict=True,
            memo=f"Pembatalan nota kredit {note.number}")
    except (LedgerError, StockError) as error:
        raise CreditNoteError(str(error)) from error
    note.status = CreditNote.STATUS_VOID
    note.save(update_fields=["status"])
    CustomerCredit.objects.filter(credit_note=note).update(voided=True)
    refresh_status_after_void(note.invoice)
    return note


def refresh_status_after_void(invoice):
    """A voided note no longer counts: the invoice may be open again."""
    if invoice.status != Invoice.STATUS_VOID:
        invoice.status = status_for(invoice)
        invoice.save(update_fields=["status"])


@register_backlog
def post_unposted_credit_notes(limit=200):
    posted = 0
    notes = CreditNote.objects.exclude(status=CreditNote.STATUS_VOID).select_related(
        "store", "invoice__order", "bank_account__account")[:limit]
    for note in unposted(notes, "credit_note"):
        try:
            with transaction.atomic():
                _post(note)
            posted += 1
        except LedgerError:
            logger.exception("ledger posting failed for credit note %s", note.pk)
    return posted
