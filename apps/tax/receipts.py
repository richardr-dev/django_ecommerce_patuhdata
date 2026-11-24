"""Customer payments beyond one invoice at a time.

A receipt (terima pembayaran) records one transfer and spreads it over several invoices of the
same buyer; what no invoice takes is kept as the buyer's credit (uang muka). Credit is a
liability (Uang muka & kredit pelanggan): it can pay a later invoice or be refunded. Each
allocation is an ordinary invoice payment, so invoice statuses and the receivable follow.

Uang muka kena PPN (``advance_vat``, PKP only): money received before the goods are handed over is a
PPN moment of its own (faktur uang muka). The PPN inside the deposit goes to PPN keluaran on the day
it is received and only the rest to Kredit pelanggan; ``CustomerCredit.vat`` carries that PPN. When the
credit pays an invoice, its share of the PPN comes back off PPN keluaran (``InvoicePayment.vat_settled``)
because the invoice books PPN on the whole sale; a refund takes its share off the same way (the faktur
uang muka must then be cancelled or replaced in Coretax)."""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from apps.accounting.ledger import (
    CASH,
    CUSTOMER_CREDIT,
    VAT,
    LedgerError,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from apps.accounting.models import BankStatementLine

from .models import CustomerCredit, CustomerReceipt, Invoice, InvoicePayment
from .numbering import next_number
from .posting import status_for
from .services import InvoiceError, record_payment

logger = logging.getLogger(__name__)

OPEN = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]


class ReceiptError(ValueError):
    pass


def invoice_party(invoice):
    """(customer, buyer_name) an invoice's credit belongs to."""
    return invoice.order.customer, invoice.buyer_name


def _party_filter(customer, buyer_name):
    return Q(customer=customer) if customer else Q(customer__isnull=True, buyer_name__iexact=buyer_name)


def credit_balance(store, customer, buyer_name):
    rows = CustomerCredit.objects.filter(_party_filter(customer, buyer_name), store=store,
        voided=False)
    return rows.aggregate(total=Sum("amount", default=0))["total"]


def credit_balances(store):
    """Everyone holding credit: ``[{"customer", "name", "balance"}]``, largest first."""
    parties = {}
    for row in CustomerCredit.objects.filter(store=store, voided=False).select_related("customer"):
        key = f"c{row.customer_id}" if row.customer_id else f"n{row.buyer_name.lower()}"
        party = parties.setdefault(key, {"customer": row.customer, "name": row.customer.name
            if row.customer_id else row.buyer_name, "balance": Decimal(0)})
        party["balance"] += row.amount
    return sorted((p for p in parties.values() if p["balance"] != 0), key=lambda p: -p["balance"])


def open_invoices_for(store, customer, buyer_name):
    invoices = Invoice.objects.filter(store=store, status__in=OPEN).select_related("order")
    invoices = (invoices.filter(order__customer=customer) if customer else
        invoices.filter(order__customer__isnull=True, buyer_name__iexact=buyer_name))
    return [inv for inv in invoices.order_by("due_date", "issued_on") if inv.amount_due > 0]


def auto_allocate(invoices, amount):
    """Oldest-due first: ``[(invoice, amount)]``."""
    out, left = [], Decimal(amount)
    for invoice in invoices:
        if left <= 0:
            break
        part = min(left, invoice.amount_due)
        out.append((invoice, part))
        left -= part
    return out


@transaction.atomic
def receive_payment(store, *, customer, buyer_name, date, amount, method, allocations,
        reference="", bank_account=None, note="", keep_rest=True, advance_vat=False, actor=None):
    """Record ``amount`` received and spread it over ``allocations``: ``[(invoice, amount)]`` or
    ``[(invoice, amount, withheld)]``, where ``withheld`` is PPh the buyer kept back on that invoice (bukti potong).
    The money received covers only the amounts; each invoice is settled by its amount plus its withholding."""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ReceiptError("Jumlah yang diterima harus lebih dari nol.")
    allocations = [(a[0], Decimal(str(a[1] or 0)), Decimal(str(a[2] if len(a) > 2 and a[2] else 0)))
        for a in allocations]
    if any(part < 0 or withheld < 0 for _, part, withheld in allocations):
        raise ReceiptError("Alokasi dan PPh dipotong tidak boleh negatif.")
    allocations = [a for a in allocations if a[1] > 0 or a[2] > 0]
    allocated = sum((part for _, part, _w in allocations), Decimal(0))
    if allocated > amount:
        raise ReceiptError(f"Alokasi (Rp{allocated:,.0f}) melebihi jumlah diterima "
            f"(Rp{amount:,.0f}).".replace(",", "."))
    rest = amount - allocated
    if rest and not keep_rest:
        raise ReceiptError(f"Rp{rest:,.0f} belum dialokasikan ke invoice.".replace(",", "."))
    if bank_account and bank_account.store_id != store.id:
        raise ReceiptError("Rekening tidak ditemukan.")
    deposit_vat = advance_vat_of(store, rest) if advance_vat and rest else Decimal(0)
    if any(inv.store_id != store.id for inv, _p, _w in allocations):
        raise ReceiptError("Invoice tidak ditemukan.")
    receipt = CustomerReceipt.objects.create(store=store, customer=customer,
        buyer_name=buyer_name, number=next_number(store, "RC", date), date=date, amount=amount,
        deposit=rest, deposit_vat=deposit_vat, method=method, reference=reference[:120], bank_account=bank_account,
        note=note[:200], created_by=actor)
    for invoice, part, withheld in allocations:
        try:
            record_payment(invoice, part, date, method, reference=reference, note=note,
                user=actor, bank_account=bank_account, receipt=receipt, withheld=withheld)
        except InvoiceError as error:
            raise ReceiptError(f"{invoice.number}: {error}") from error
    if rest:
        _post_deposit(receipt, strict=True, actor=actor)
        CustomerCredit.objects.create(store=store, customer=customer, buyer_name=buyer_name,
            kind="deposit", date=date, amount=rest, vat=deposit_vat, receipt=receipt, created_by=actor,
            note=f"Uang muka kena PPN {receipt.number}" if deposit_vat else f"Penerimaan {receipt.number}")
    return receipt


def advance_vat_of(store, amount):
    """The PPN inside a down payment of ``amount`` (PPN included). Only a PKP charges it."""
    from .services import exclusive_price, tax_profile
    if not tax_profile(store).is_pkp or not store.tax_rate:
        raise ReceiptError("Uang muka kena PPN hanya untuk PKP dengan tarif PPN di profil pajak.")
    return Decimal(amount) - exclusive_price(store, amount, is_pkp=True)


def credit_vat_balance(store, customer, buyer_name):
    rows = CustomerCredit.objects.filter(_party_filter(customer, buyer_name), store=store, voided=False)
    return rows.aggregate(total=Sum("vat", default=0))["total"]


def _vat_share(store, customer, buyer_name, amount, balance):
    """The PPN that leaves with ``amount`` of a credit balance: in proportion, all of it with the last rupiah."""
    vat = credit_vat_balance(store, customer, buyer_name)
    if vat <= 0:
        return Decimal(0)
    if amount >= balance:
        return vat
    return min(vat, (vat * amount / balance).quantize(Decimal("0.01")))


def _post_deposit(receipt, *, strict=False, actor=None):
    cash = receipt.bank_account.account.code if receipt.bank_account_id else CASH
    return post_entry(receipt.store, date=receipt.date, strict=strict, actor=actor,
        source=("receipt_deposit", receipt.pk),
        memo=f"Uang muka {receipt.buyer_name} · {receipt.number}",
        amounts={cash: receipt.deposit, CUSTOMER_CREDIT: -(receipt.deposit - receipt.deposit_vat),
            VAT: -receipt.deposit_vat})


@transaction.atomic
def apply_credit(invoice, amount, date, *, actor=None):
    """Pay (part of) an invoice from the buyer's credit."""
    amount = Decimal(str(amount))
    customer, name = invoice_party(invoice)
    balance = credit_balance(invoice.store, customer, name)
    if amount <= 0:
        raise ReceiptError("Jumlah harus lebih dari nol.")
    if amount > balance:
        raise ReceiptError(f"Kredit pelanggan hanya Rp{balance:,.0f}.".replace(",", "."))
    vat = _vat_share(invoice.store, customer, name, amount, balance)
    try:
        payment = record_payment(invoice, amount, date, InvoicePayment.METHOD_CREDIT,
            note="Dibayar dari uang muka kena PPN" if vat else "Dibayar dari kredit pelanggan", user=actor,
            vat_settled=vat)
    except InvoiceError as error:
        raise ReceiptError(str(error)) from error
    CustomerCredit.objects.create(store=invoice.store, customer=customer, buyer_name=name,
        kind="applied", date=date, amount=-amount, vat=-vat, invoice=invoice, payment=payment,
        created_by=actor, note=f"Dipakai untuk {invoice.number}")
    return payment


@transaction.atomic
def refund_credit(store, customer, buyer_name, amount, date, *, bank_account=None, actor=None):
    """Hand the buyer's credit back as money."""
    amount = Decimal(str(amount))
    balance = credit_balance(store, customer, buyer_name)
    if amount <= 0 or amount > balance:
        raise ReceiptError(f"Jumlah harus antara 1 dan Rp{balance:,.0f}.".replace(",", "."))
    vat = _vat_share(store, customer, buyer_name, amount, balance)
    row = CustomerCredit.objects.create(store=store, customer=customer, buyer_name=buyer_name,
        kind="refund", date=date, amount=-amount, vat=-vat, created_by=actor,
        note="Pengembalian uang muka kena PPN" if vat else "Pengembalian kredit pelanggan")
    cash = bank_account.account.code if bank_account else CASH
    try:
        post_entry(store, date=date, strict=True, actor=actor, source=("credit_refund", row.pk),
            memo=f"Pengembalian kredit {buyer_name}", amounts={CUSTOMER_CREDIT: amount - vat, VAT: vat,
                cash: -amount})
    except LedgerError as error:
        raise ReceiptError(str(error)) from error
    return row


@transaction.atomic
def void_receipt(receipt, *, actor=None):
    """Undo a receipt: its payments leave the invoices (which reopen) and its credit goes."""
    receipt = CustomerReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.voided:
        return receipt
    payments = list(receipt.allocations.select_related("invoice"))
    sources = [("invoice_payment", str(p.pk)) for p in payments]
    if receipt.deposit:
        sources.append(("receipt_deposit", str(receipt.pk)))
    for kind, key in sources:
        if BankStatementLine.objects.filter(matched_line__entry__source_type=kind,
                matched_line__entry__source_id=key, matched_line__entry__superseded=False).exists():
            raise ReceiptError("Penerimaan ini sudah dicocokkan dengan mutasi bank. Batalkan "
                "pencocokannya dulu di Rekonsiliasi bank.")
    memo = f"Pembatalan penerimaan {receipt.number}"
    try:
        for payment in payments:
            reverse_source(receipt.store, "invoice_payment", payment.pk, strict=True,
                actor=actor, memo=memo)
        if receipt.deposit:
            reverse_source(receipt.store, "receipt_deposit", receipt.pk, strict=True,
                actor=actor, memo=memo)
    except LedgerError as error:
        raise ReceiptError(str(error)) from error
    invoices = {p.invoice for p in payments}
    for payment in payments:
        payment.delete()
    for invoice in invoices:
        _reopen(invoice)
    CustomerCredit.objects.filter(receipt=receipt).update(voided=True)
    receipt.voided = True
    receipt.save(update_fields=["voided"])
    return receipt


@transaction.atomic
def release_to_credit(receipt, *, actor=None):
    """Lepas ke kredit pelanggan: the money did arrive, but it was put on the wrong invoices. The
    payments leave the invoices (which reopen) and the whole amount becomes the buyer's credit, to
    apply to the right invoice or refund. The bank is untouched: each payment entry is reversed and
    the receipt's deposit entry is booked again for the larger amount (Kas/bank / Kredit pelanggan),
    so the net change is Piutang usaha up and Kredit pelanggan up. Returns the amount released."""
    receipt = CustomerReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.voided:
        raise ReceiptError("Penerimaan ini sudah dibatalkan.")
    payments = list(receipt.allocations.select_related("invoice"))
    if not payments:
        raise ReceiptError("Penerimaan ini tidak dialokasikan ke invoice; seluruhnya sudah kredit pelanggan.")
    if any(payment.withheld for payment in payments):
        raise ReceiptError("Ada PPh yang dipotong pembeli pada penerimaan ini. Bukti potongnya milik invoice "
            "itu, jadi batalkan penerimaan lalu catat ulang.")
    sources = [("invoice_payment", str(p.pk)) for p in payments]
    if receipt.deposit:
        sources.append(("receipt_deposit", str(receipt.pk)))
    for kind, key in sources:
        if BankStatementLine.objects.filter(matched_line__entry__source_type=kind,
                matched_line__entry__source_id=key, matched_line__entry__superseded=False).exists():
            raise ReceiptError("Penerimaan ini sudah dicocokkan dengan mutasi bank. Batalkan "
                "pencocokannya dulu di Rekonsiliasi bank.")
    released = sum((payment.amount for payment in payments), Decimal(0))
    memo = f"Alokasi {receipt.number} dilepas ke kredit pelanggan"
    try:
        for payment in payments:
            reverse_source(receipt.store, "invoice_payment", payment.pk, strict=True, actor=actor, memo=memo)
        if receipt.deposit:
            reverse_source(receipt.store, "receipt_deposit", receipt.pk, strict=True, actor=actor, memo=memo)
        invoices = {payment.invoice for payment in payments}
        numbers = ", ".join(sorted(invoice.number for invoice in invoices))
        for payment in payments:
            payment.delete()
        for invoice in invoices:
            _reopen(invoice)
        receipt.deposit += released
        receipt.save(update_fields=["deposit"])
        _post_deposit(receipt, strict=True, actor=actor)
    except LedgerError as error:
        raise ReceiptError(str(error)) from error
    CustomerCredit.objects.create(store=receipt.store, customer=receipt.customer, buyer_name=receipt.buyer_name,
        kind="deposit", date=receipt.date, amount=released, receipt=receipt, created_by=actor,
        note=f"Dilepas dari {numbers} ({receipt.number})")
    return released


def _reopen(invoice):
    invoice.status = status_for(invoice)
    invoice.save(update_fields=["status"])


@register_backlog
def post_unposted_deposits(limit=200):
    posted = 0
    deposits = CustomerReceipt.objects.filter(voided=False, deposit__gt=0).select_related(
        "store", "bank_account__account")[:limit]
    for receipt in unposted(deposits, "receipt_deposit"):
        try:
            with transaction.atomic():
                _post_deposit(receipt)
            posted += 1
        except LedgerError:
            logger.exception("ledger posting failed for receipt %s", receipt.pk)
    return posted

