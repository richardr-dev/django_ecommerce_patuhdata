"""Utang usaha: supplier bills and their payments, each posted to the ledger as it happens.

A bill books what was bought (stock or an expense) against Utang usaha; a payment moves the
debt out of the bank. Voiding reverses the entry instead of deleting it."""
from decimal import Decimal

from django.db import transaction

from .ledger import (
    CASH,
    EQUITY,
    GRNI,
    INPUT_VAT,
    PAYABLE,
    PURCHASE_VARIANCE,
    WITHHELD,
    LedgerError,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from .models import Bill, BillItem, BillPayment

UNPOSTED_STATUSES = {Bill.STATUS_DRAFT, Bill.STATUS_VOID}


class PayableError(ValueError):
    pass


def _can_credit_input_vat(store):
    from apps.tax.services import tax_profile  # tax builds on accounting, so import late

    return tax_profile(store).is_pkp


def post_bill(bill, *, strict=False, actor=None):
    """Only a PKP may credit PPN masukan; for everyone else the PPN is part of the cost."""
    if bill.is_opening:
        # Carried over from the old books: the purchase was booked there, so only the debt comes over,
        # on the day these books start.
        from .opening import existing
        start = existing(bill.store)
        return post_entry(bill.store, date=start.date if start else bill.bill_date, actor=actor,
            source=("bill", bill.pk), memo=f"Saldo awal utang {bill.supplier.name} {bill.number}".strip(),
            amounts={EQUITY: bill.total, PAYABLE: -bill.total})
    vat = bill.tax if _can_credit_input_vat(bill.store) else Decimal(0)
    memo = f"Tagihan {bill.supplier.name} {bill.number}".strip()
    receipt = getattr(bill, "goods_receipt", None)
    if receipt:
        # The goods were booked at receipt: the bill clears GRNI, and only the price difference
        # (and the PPN a non-PKP cannot credit) is new cost.
        received = receipt.value
        amounts = {GRNI: received, PURCHASE_VARIANCE: bill.total - vat - received, INPUT_VAT: vat,
            PAYABLE: -bill.total}
    else:
        amounts = {bill.category: bill.total - vat, INPUT_VAT: vat, PAYABLE: -bill.total}
    return post_entry(bill.store, date=bill.bill_date, strict=strict, actor=actor,
        source=("bill", bill.pk), cost_center=bill.cost_center, memo=memo, amounts=amounts)


def post_bill_payment(payment, *, strict=False, actor=None):
    bill = payment.bill
    return post_entry(bill.store, date=payment.paid_on, strict=strict, actor=actor,
        source=("bill_payment", payment.pk),
        memo=f"Bayar tagihan {bill.supplier.name} {bill.number}".strip(),
        amounts={PAYABLE: payment.amount, WITHHELD: -payment.withheld,
            (payment.bank_account.account.code if payment.bank_account_id else CASH):
                -(payment.amount - payment.withheld)})


def _find_goods(store, sku):
    """The product (and variant) a bill line buys, from its SKU; (None, None) for a line that
    is not goods."""
    from apps.catalog.models import Product, ProductVariant

    sku = (sku or "").strip()
    if not sku:
        return None, None
    variant = ProductVariant.objects.select_related("product").filter(product__store=store,
        sku=sku).first()
    if variant:
        return variant.product, variant
    return Product.objects.filter(store=store, sku=sku).first(), None


@transaction.atomic
def receive_bill(bill, *, warehouse=None, actor=None):
    """Terima barang: put the goods a bill bought on the shelf and fold their cost into each
    item's average cost. Lines already received are skipped, so it is safe to repeat.
    Returns the number of units received."""
    from apps.orders.stock import StockError, receive_goods

    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status in UNPOSTED_STATUSES:
        raise PayableError("Tagihan ini tidak bisa diterima barangnya.")
    # What the supplier charged beyond the lines is part of what the goods cost, unless the
    # seller credits it back as PPN masukan.
    uplift = Decimal(0) if _can_credit_input_vat(bill.store) or not bill.subtotal else (
        bill.tax / bill.subtotal)
    received = 0
    for item in bill.items.select_for_update(of=("self",)).select_related("product", "variant"):
        if not item.product_id:
            continue
        if item.quantity != int(item.quantity):
            raise PayableError(f"Jumlah barang {item.description} harus bilangan bulat.")
        pending = int(item.quantity) - item.received_quantity
        if pending <= 0:
            continue
        unit_cost = (item.unit_price * (1 + uplift)).quantize(Decimal("0.01"))
        try:
            receive_goods(store=bill.store, product=item.product, variant=item.variant,
                quantity=pending, unit_cost=unit_cost, warehouse=warehouse, actor=actor,
                reason=f"Barang masuk dari {bill.supplier.name} {bill.number}".strip(),
                source=("bill", bill.pk))
        except StockError as error:
            raise PayableError(str(error)) from error
        item.received_quantity += pending
        item.save(update_fields=["received_quantity"])
        received += pending
    return received


@transaction.atomic
def create_bill(store, *, supplier, lines, bill_date, due_date=None, number="", tax=Decimal(0),
        category="1400", notes="", actor=None, cost_center=None, receipt=None, opening=False):
    """``lines`` is a list of dicts with description, qty, unit_price. ``receipt`` is the goods
    receipt this bill invoices: its stock is already on the shelf, so the lines count as received."""
    if category not in dict(Bill._meta.get_field("category").choices):
        raise PayableError("Kategori tagihan tidak dikenal.")
    if not lines:
        raise PayableError("Tambahkan minimal satu item.")
    if tax < 0:
        raise PayableError("PPN tidak boleh negatif.")
    for line in lines:
        line["product"], line["variant"] = _find_goods(store, line.get("sku", ""))
        if line.get("sku") and not line["product"]:
            raise PayableError(f"SKU {line['sku']} tidak ada di katalog.")
    subtotal = sum((line["qty"] * line["unit_price"] for line in lines), Decimal(0))
    if subtotal <= 0:
        raise PayableError("Total tagihan harus lebih dari nol.")
    bill = Bill.objects.create(store=store, supplier=supplier, number=number, category=category,
        bill_date=bill_date, due_date=due_date, subtotal=subtotal, tax=tax, total=subtotal + tax,
        notes=notes, created_by=actor, cost_center=cost_center, is_opening=opening)
    BillItem.objects.bulk_create(BillItem(bill=bill, description=line["description"],
        quantity=line["qty"], unit_price=line["unit_price"], product=line["product"],
        variant=line["variant"], line_total=line["qty"] * line["unit_price"],
        received_quantity=int(line["qty"]) if receipt and line["product"] else 0) for line in lines)
    if receipt:
        receipt.bill = bill
        receipt.status = "billed"
        receipt.save(update_fields=["bill", "status"])
        bill = Bill.objects.get(pk=bill.pk)  # so bill.goods_receipt resolves
    try:
        post_bill(bill, strict=True, actor=actor)
    except LedgerError as error:
        raise PayableError(str(error)) from error
    return bill


@transaction.atomic
def pay_bill(bill, *, amount, paid_on, method="transfer", reference="", actor=None,
        bank_account=None, withholding=None):
    """``withholding`` is ``(kind, rate)``: PPh held back from this payment (on the part before PPN)."""
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status in UNPOSTED_STATUSES:
        raise PayableError("Tagihan ini tidak bisa dibayar.")
    if amount <= 0:
        raise PayableError("Jumlah pembayaran harus lebih dari nol.")
    if amount > bill.amount_due:
        raise PayableError(f"Pembayaran melebihi sisa tagihan (Rp{bill.amount_due:,.0f}).")
    payment = BillPayment.objects.create(bill=bill, paid_on=paid_on, amount=amount,
        method=method, reference=reference, recorded_by=actor, bank_account=bank_account)
    if withholding:
        from . import withholding as pph
        kind, rate = withholding
        base = (amount * bill.subtotal / bill.total).quantize(Decimal(1)) if bill.total else amount
        try:
            held = pph.record(bill.store, kind=kind, date=paid_on, base=base, rate=rate,
                payee_name=bill.supplier.name, payee_npwp=bill.supplier.npwp, source_type="bill_payment",
                source_id=payment.pk, actor=actor)
        except pph.WithholdingError as error:
            raise PayableError(str(error)) from error
        if held.amount >= amount:
            raise PayableError("PPh yang dipotong tidak boleh sebesar pembayaran.")
        payment.withheld = held.amount
        payment.save(update_fields=["withheld"])
    try:
        post_bill(bill, actor=actor)  # bills older than ledger posting
        post_bill_payment(payment, strict=True, actor=actor)
    except LedgerError as error:
        raise PayableError(str(error)) from error
    bill.status = Bill.STATUS_PAID if bill.amount_due <= 0 else Bill.STATUS_PARTIAL
    bill.save(update_fields=["status"])
    return payment


@transaction.atomic
def void_bill(bill, *, actor=None):
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status == Bill.STATUS_VOID:
        return bill
    if bill.payments.exists():
        raise PayableError("Tagihan yang sudah dibayar tidak bisa dibatalkan.")
    try:
        reverse_source(bill.store, "bill", bill.pk, actor=actor,
            memo=f"Pembatalan tagihan {bill.supplier.name} {bill.number}".strip())
    except LedgerError as error:
        raise PayableError(str(error)) from error
    bill.status = Bill.STATUS_VOID
    bill.save(update_fields=["status"])
    from .receipts import reopen_after_void
    reopen_after_void(bill)
    return bill


@register_backlog
def post_unposted_bills():
    posted = 0
    bills = Bill.objects.exclude(status__in=UNPOSTED_STATUSES)
    for bill in unposted(bills, "bill"):
        posted += bool(post_bill(bill))
    payments = BillPayment.objects.exclude(bill__status__in=UNPOSTED_STATUSES)
    for payment in unposted(payments, "bill_payment"):
        posted += bool(post_bill_payment(payment))
    return posted


# ---------------------------------------------------------------------------
# Umur utang: what is owed to each supplier, and for how long
# ---------------------------------------------------------------------------

AGING_BUCKETS = [("current", "Belum jatuh tempo"), ("d30", "1–30 hari"), ("d60", "31–60 hari"),
    ("d90", "61–90 hari"), ("over", "Lebih dari 90 hari")]


def _bucket(bill, as_of):
    late = (as_of - (bill.due_date or bill.bill_date)).days
    return ("current" if late <= 0 else "d30" if late <= 30 else "d60" if late <= 60
        else "d90" if late <= 90 else "over")


def open_bills(store, *, supplier_id=None):
    bills = Bill.objects.filter(store=store, status__in=[Bill.STATUS_OPEN, Bill.STATUS_PARTIAL,
        Bill.STATUS_OVERDUE]).select_related("supplier").prefetch_related("payments")
    if supplier_id:
        bills = bills.filter(supplier_id=supplier_id)
    return [bill for bill in bills.order_by("due_date", "bill_date") if bill.amount_due > 0]


def aging(store, as_of):
    """Open balance per supplier split into age buckets, largest first. Matches the
    Utang usaha account for bills."""
    parties = {}
    for bill in open_bills(store):
        row = parties.setdefault(bill.supplier_id, {"name": bill.supplier.name,
            "supplier_id": bill.supplier_id, "total": Decimal(0), "bills": 0,
            **{key: Decimal(0) for key, _ in AGING_BUCKETS}})
        due = bill.amount_due
        row[_bucket(bill, as_of)] += due
        row["total"] += due
        row["bills"] += 1
    rows = sorted(parties.values(), key=lambda row: -row["total"])
    totals = {key: sum((row[key] for row in rows), Decimal(0)) for key, _ in AGING_BUCKETS}
    totals["total"] = sum((row["total"] for row in rows), Decimal(0))
    return rows, totals


def due_soon(store, as_of, days=7):
    """Bills to pay within ``days``, soonest first (overdue ones lead)."""
    from datetime import timedelta
    horizon = as_of + timedelta(days=days)
    return [bill for bill in open_bills(store) if bill.due_date and bill.due_date <= horizon]
