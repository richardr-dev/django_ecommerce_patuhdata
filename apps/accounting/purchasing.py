"""Pesanan pembelian (purchase order) → penerimaan barang (partial, repeatable) → tagihan.

A PO books nothing. Each goods receipt against it moves the ledger (Persediaan / GRNI) and adds to the
line's ``received_quantity``; the bill is made from a receipt (``receipts.bill_receipt``) and clears GRNI,
with any price difference to Selisih harga pembelian. So a line is matched three ways: ordered (PO),
received (receipts), billed (bills of those receipts), and the page shows all three. Receiving more than
is open on a line is refused; the rest of an order can be closed when the supplier will not deliver it."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import PurchaseOrder, PurchaseOrderLine
from .payables import _find_goods


class PurchaseError(ValueError):
    pass


def next_number(store, order_date):
    prefix = f"PO/{order_date:%Y}/"
    last = PurchaseOrder.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    n = int(last.number.rsplit("/", 1)[1]) + 1 if last else 1
    return f"{prefix}{n:04d}"


@transaction.atomic
def create_po(store, *, supplier, lines, order_date, expected_on=None, notes="", actor=None):
    """``lines``: dicts with sku, qty (whole units), unit_cost."""
    if not lines:
        raise PurchaseError("Tambahkan minimal satu barang.")
    if expected_on and expected_on < order_date:
        raise PurchaseError("Tanggal perkiraan tiba tidak boleh sebelum tanggal pesanan.")
    resolved = []
    for line in lines:
        product, variant = _find_goods(store, line.get("sku", ""))
        if not product:
            raise PurchaseError(f"SKU {line.get('sku') or '(kosong)'} tidak ada di katalog.")
        qty, cost = line["qty"], line["unit_cost"]
        if qty != int(qty) or qty <= 0:
            raise PurchaseError(f"Jumlah {product.name} harus bilangan bulat lebih dari nol.")
        if cost < 0:
            raise PurchaseError("Harga tidak boleh negatif.")
        resolved.append((product, variant, int(qty), cost))
    po = PurchaseOrder.objects.create(store=store, supplier=supplier, number=next_number(store, order_date),
        order_date=order_date, expected_on=expected_on, notes=notes, created_by=actor)
    PurchaseOrderLine.objects.bulk_create(PurchaseOrderLine(po=po, product=product, variant=variant,
        description=f"{product.name} · {variant.name}" if variant else product.name, quantity=qty,
        unit_cost=cost) for product, variant, qty, cost in resolved)
    return po


def take_against_po(po, supplier, picks):
    """Reserve ``picks`` = ``[(po_line, quantity)]`` of a purchase order for a receipt being made.
    Locks the lines, refuses a closed order, a foreign line or more than is open; updates the status."""
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if not po.is_receivable:
        raise PurchaseError(f"Pesanan {po.number} sudah {po.get_status_display().lower()}.")
    if po.supplier_id != supplier.pk:
        raise PurchaseError("Supplier penerimaan harus sama dengan supplier pesanan.")
    for po_line, quantity in picks:
        if po_line is None:
            raise PurchaseError("Setiap baris penerimaan harus merujuk ke baris pesanan.")
        locked = PurchaseOrderLine.objects.select_for_update().get(pk=po_line.pk)
        if locked.po_id != po.pk:
            raise PurchaseError("Baris bukan milik pesanan ini.")
        if quantity > locked.remaining:
            raise PurchaseError(f"{locked.description}: diterima {quantity}, tersisa di pesanan {locked.remaining}.")
        locked.received_quantity += quantity
        locked.save(update_fields=["received_quantity"])
    refresh_status(po)


def refresh_status(po):
    lines = list(po.lines.all())
    if all(line.received_quantity >= line.quantity for line in lines):
        status = PurchaseOrder.RECEIVED
    elif any(line.received_quantity for line in lines):
        status = PurchaseOrder.PARTIAL
    else:
        status = PurchaseOrder.OPEN
    if status != po.status:
        po.status = status
        po.save(update_fields=["status"])


@transaction.atomic
def receive_po(po, *, quantities, received_on, number="", warehouse=None, notes="", actor=None):
    """Receive part (or all) of a purchase order. ``quantities`` maps a PO line id to the units that
    arrived; lines at zero are left out. Returns the goods receipt."""
    from .receipts import ReceiptError, create_receipt
    po = PurchaseOrder.objects.select_related("supplier").get(pk=po.pk)
    lines = []
    for line in po.lines.select_related("product", "variant"):
        qty = quantities.get(line.pk, 0)
        if qty:
            sku = line.variant.sku if line.variant and line.variant.sku else line.product.sku
            lines.append({"sku": sku, "qty": Decimal(qty), "unit_cost": line.unit_cost, "po_line": line})
    if not lines:
        raise PurchaseError("Isi jumlah yang diterima untuk minimal satu baris.")
    try:
        return create_receipt(po.store, supplier=po.supplier, lines=lines, received_on=received_on,
            number=number, warehouse=warehouse, notes=notes, actor=actor, purchase_order=po)
    except ReceiptError as error:
        raise PurchaseError(str(error)) from error


@transaction.atomic
def close_po(po, *, actor=None):
    """Stop waiting for what has not arrived. Nothing received: the order is cancelled."""
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if not po.is_receivable:
        raise PurchaseError("Pesanan ini sudah selesai.")
    po.status = PurchaseOrder.CLOSED if po.lines.filter(received_quantity__gt=0).exists() else PurchaseOrder.CANCELLED
    po.save(update_fields=["status"])
    return po


def overdue(store, today=None):
    today = today or timezone.localdate()
    return PurchaseOrder.objects.filter(store=store, status__in=[PurchaseOrder.OPEN, PurchaseOrder.PARTIAL],
        expected_on__lt=today)


def waiting_value(store):
    """What is still on order: the open quantity of every line, at its price."""
    total = Decimal(0)
    for po in PurchaseOrder.objects.filter(store=store, status__in=[PurchaseOrder.OPEN, PurchaseOrder.PARTIAL]
            ).prefetch_related("lines"):
        total += sum((line.remaining * line.unit_cost for line in po.lines.all()), Decimal(0))
    return total


__all__ = ["PurchaseError", "close_po", "create_po", "overdue", "receive_po", "waiting_value"]
