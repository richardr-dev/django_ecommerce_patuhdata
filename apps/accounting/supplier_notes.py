"""Supplier side of a bill after it is booked: nota debit (goods or a charge sent back) and one payment
over several bills.

A debit note against bill B of supplier S, for returned goods:
    Utang usaha (debit)  /  Persediaan (credit, at the bill's price)  /  PPN masukan (credit, if B credited it)
and the returned units leave the shelf (``purchase_return`` movement). For a non-goods bill the credit
goes to the bill's expense account and no stock moves. A non-PKP never credited PPN, so the whole
amount comes off the cost. The note lowers ``Bill.amount_due``; it cannot exceed what is unpaid, and
money already paid back by the supplier is an ordinary cash entry, not a note."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from .ledger import INPUT_VAT, PAYABLE, LedgerError, money, post_entry, register_backlog, unposted
from .models import Bill, BillItem, DebitNote, DebitNoteItem
from .payables import PayableError, _can_credit_input_vat, pay_bill


class NoteError(ValueError):
    pass


def next_number(store, note_date):
    prefix = f"DN/{note_date:%Y}/"
    last = DebitNote.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('/', 1)[1]) + 1) if last else 1:04d}"


def returned(bill_item):
    return bill_item.returns.filter(note__status="issued").aggregate(q=Sum("quantity", default=0))["q"]


def returnable(bill_item):
    """Units of this line that can still go back: received goods, or any unreturned quantity of a charge."""
    base = bill_item.received_quantity if bill_item.product_id else bill_item.quantity
    return Decimal(base) - returned(bill_item)


def post_debit_note(note, *, strict=False, actor=None):
    bill = note.bill
    vat = note.tax if _can_credit_input_vat(bill.store) else Decimal(0)
    return post_entry(bill.store, date=note.note_date, strict=strict, actor=actor, source=("debit_note", note.pk),
        cost_center=bill.cost_center, memo=f"Nota debit {note.number} {bill.supplier.name} {bill.number}".strip(),
        amounts={PAYABLE: note.total, bill.category: -(note.total - vat), INPUT_VAT: -vat})


@transaction.atomic
def create_debit_note(bill, *, quantities, reason, note_date, actor=None):
    """``quantities`` maps a BillItem id to the quantity sent back."""
    from apps.orders.stock import StockError, stock_out
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status in (Bill.STATUS_DRAFT, Bill.STATUS_VOID):
        raise NoteError("Tagihan ini tidak bisa diberi nota debit.")
    if bill.is_opening:
        raise NoteError("Tagihan saldo awal tidak memakai nota debit: bila sisanya salah, batalkan lalu catat lagi.")
    if not (reason or "").strip():
        raise NoteError("Tulis alasan retur.")
    picks = []
    for item in bill.items.select_for_update(of=("self",)).select_related("product", "variant"):
        qty = Decimal(quantities.get(item.pk, 0) or 0)
        if qty <= 0:
            continue
        if item.variant_id:
            raise NoteError(f"{item.description}: retur barang bervarian belum didukung.")
        if bill.category != "1400" and item.product_id:
            raise NoteError(f"{item.description}: barang pada tagihan non-persediaan tidak dikembalikan lewat stok.")
        if item.product_id and qty != int(qty):
            raise NoteError(f"{item.description}: jumlah retur harus bilangan bulat.")
        if qty > returnable(item):
            raise NoteError(f"{item.description}: retur {qty:g}, maksimal {returnable(item):g}"
                f"{' (hanya barang yang sudah diterima)' if item.product_id else ''}.")
        picks.append((item, qty))
    if not picks:
        raise NoteError("Isi jumlah retur untuk minimal satu baris.")
    subtotal = sum((qty * item.unit_price for item, qty in picks), Decimal(0))
    tax = money(subtotal * bill.tax / bill.subtotal) if bill.subtotal and bill.tax else Decimal(0)
    tax = Decimal(tax).quantize(Decimal(1), ROUND_HALF_UP)
    total = subtotal + tax
    if total > bill.amount_due:
        raise NoteError(f"Nota debit Rp{total:,.0f} melebihi sisa tagihan Rp{bill.amount_due:,.0f}. Uang yang sudah "
            "dibayar dikembalikan supplier dicatat sebagai penerimaan kas.".replace(",", "."))
    note = DebitNote.objects.create(store=bill.store, supplier=bill.supplier, bill=bill, reason=reason.strip()[:200],
        number=next_number(bill.store, note_date), note_date=note_date, subtotal=subtotal, tax=tax, total=total,
        created_by=actor)
    DebitNoteItem.objects.bulk_create(DebitNoteItem(note=note, bill_item=item, description=item.description, quantity=qty,
        unit_price=item.unit_price, line_total=qty * item.unit_price) for item, qty in picks)
    try:
        for item, qty in picks:
            if item.product_id:
                stock_out(store=bill.store, product=item.product, quantity=int(qty), kind="purchase_return",
                    reason=f"Retur ke {bill.supplier.name} {note.number}"[:200], source=("debit_note", str(note.pk)),
                    unit_cost=item.unit_price, actor=actor)
        post_debit_note(note, strict=True, actor=actor)
    except (StockError, LedgerError) as error:
        raise NoteError(str(error)) from error
    bill = Bill.objects.get(pk=bill.pk)
    bill.status = (Bill.STATUS_PAID if bill.amount_due <= 0
        else Bill.STATUS_PARTIAL if bill.amount_paid or bill.amount_credited else Bill.STATUS_OPEN)
    bill.save(update_fields=["status"])
    return note


@transaction.atomic
def pay_bills(allocations, *, paid_on, method="transfer", reference="", actor=None, bank_account=None):
    """One payment to a supplier spread over several of its bills: ``allocations`` is ``[(bill, amount)]``.
    Each bill gets its own payment and journal (so every bill settles on its own); all or nothing."""
    allocations = [(bill, Decimal(amount)) for bill, amount in allocations if Decimal(amount) > 0]
    if not allocations:
        raise NoteError("Isi jumlah bayar untuk minimal satu tagihan.")
    if len({bill.supplier_id for bill, _ in allocations}) != 1:
        raise NoteError("Satu pembayaran hanya untuk satu supplier.")
    payments = []
    for bill, amount in allocations:
        try:
            payments.append(pay_bill(bill, amount=amount, paid_on=paid_on, method=method, actor=actor,
                bank_account=bank_account, reference=reference))
        except PayableError as error:
            raise NoteError(f"{bill.number or str(bill.pk)[:8]}: {error}") from error
    return payments


@register_backlog
def post_unposted_debit_notes():
    posted = 0
    for note in unposted(DebitNote.objects.filter(status="issued").select_related("bill", "bill__supplier"), "debit_note"):
        posted += bool(post_debit_note(note))
    return posted


__all__ = ["BillItem", "NoteError", "create_debit_note", "pay_bills", "returnable"]
