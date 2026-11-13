"""Penerimaan barang (goods received before the bill) and the GRNI account.

A receipt puts the goods on the shelf and books Persediaan against "Barang diterima belum ditagih"
(GRNI, 2350). When the supplier's bill arrives it is made *from* the receipt: the bill clears GRNI
into Utang usaha instead of booking Persediaan a second time, and any difference between the bill
and the receipt price goes to "Selisih harga pembelian" (5150). Voiding that bill reopens the
receipt. A receipt itself is corrected through stock opname or a return, never edited."""
from decimal import Decimal

from django.db import transaction

from .ledger import GRNI, INVENTORY, LedgerError, post_entry, register_backlog, unposted
from .models import Bill, GoodsReceipt, GoodsReceiptLine
from .payables import PayableError, _find_goods, create_bill


class ReceiptError(ValueError):
    pass


def post_receipt(receipt, *, strict=False, actor=None):
    value = receipt.value
    return post_entry(receipt.store, date=receipt.received_on, strict=strict, actor=actor,
        source=("goods_receipt", receipt.pk),
        memo=f"Penerimaan barang {receipt.supplier.name} {receipt.number}".strip(),
        amounts={INVENTORY: value, GRNI: -value})


@transaction.atomic
def create_receipt(store, *, supplier, lines, received_on, number="", warehouse=None, notes="", actor=None,
        purchase_order=None):
    """``lines`` is a list of dicts: sku, qty (whole units), unit_cost. For a receipt against a purchase
    order each line carries ``po_line`` (a PurchaseOrderLine) and may not exceed what is still open on it."""
    from apps.orders.stock import StockError, receive_goods

    if not lines:
        raise ReceiptError("Tambahkan minimal satu barang.")
    resolved = []
    for line in lines:
        product, variant = _find_goods(store, line.get("sku", ""))
        if not product:
            raise ReceiptError(f"SKU {line.get('sku') or '(kosong)'} tidak ada di katalog.")
        qty, cost = line["qty"], line["unit_cost"]
        if qty != int(qty) or qty <= 0:
            raise ReceiptError(f"Jumlah {product.name} harus bilangan bulat lebih dari nol.")
        if cost < 0:
            raise ReceiptError("Harga tidak boleh negatif.")
        resolved.append((product, variant, int(qty), cost, line.get("po_line")))
    if purchase_order is not None:
        from .purchasing import PurchaseError, take_against_po
        try:
            take_against_po(purchase_order, supplier, [(r[4], r[2]) for r in resolved])
        except PurchaseError as error:
            raise ReceiptError(str(error)) from error
    receipt = GoodsReceipt.objects.create(store=store, supplier=supplier, number=number,
        received_on=received_on, warehouse=warehouse, notes=notes, created_by=actor,
        purchase_order=purchase_order)
    GoodsReceiptLine.objects.bulk_create(GoodsReceiptLine(receipt=receipt, product=product, variant=variant,
        description=f"{product.name} · {variant.name}" if variant else product.name, quantity=qty,
        unit_cost=cost, po_line=po_line) for product, variant, qty, cost, po_line in resolved)
    if receipt.value <= 0:
        raise ReceiptError("Nilai penerimaan harus lebih dari nol.")
    try:
        for product, variant, qty, cost, _po_line in resolved:
            receive_goods(store=store, product=product, variant=variant, quantity=qty, unit_cost=cost,
                warehouse=warehouse, actor=actor, source=("goods_receipt", receipt.pk),
                reason=f"Penerimaan barang dari {supplier.name} {number}".strip())
        post_receipt(receipt, strict=True, actor=actor)
    except (StockError, LedgerError) as error:
        raise ReceiptError(str(error)) from error
    return receipt


@transaction.atomic
def bill_receipt(receipt, *, number="", bill_date, due_date=None, tax=Decimal(0), prices=None, notes="",
        actor=None):
    """Turn a receipt into the supplier's bill. ``prices`` maps a line id to the price the supplier
    actually charged when it differs from the receipt."""
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.bill_id:
        raise ReceiptError("Penerimaan ini sudah ditagih.")
    prices = prices or {}
    lines = []
    for line in receipt.lines.select_related("product", "variant"):
        sku = (line.variant.sku if line.variant and line.variant.sku else line.product.sku)
        lines.append({"description": line.description, "qty": Decimal(line.quantity), "sku": sku,
            "unit_price": prices.get(line.pk, line.unit_cost)})
    try:
        bill = create_bill(receipt.store, supplier=receipt.supplier, lines=lines, bill_date=bill_date,
            due_date=due_date, number=number, tax=tax, category="1400", notes=notes, actor=actor,
            receipt=receipt)
    except PayableError as error:
        raise ReceiptError(str(error)) from error
    return bill


def reopen_after_void(bill):
    """A voided bill puts its receipt back to "belum ditagih"."""
    receipt = GoodsReceipt.objects.filter(bill=bill).first()
    if receipt:
        receipt.bill = None
        receipt.status = GoodsReceipt.STATUS_OPEN
        receipt.save(update_fields=["bill", "status"])


def unbilled(store):
    return GoodsReceipt.objects.filter(store=store, bill__isnull=True).select_related("supplier")


@register_backlog
def post_unposted_receipts():
    posted = 0
    for receipt in unposted(GoodsReceipt.objects.all(), "goods_receipt"):
        posted += bool(post_receipt(receipt))
    return posted


__all__ = ["Bill", "ReceiptError", "bill_receipt", "create_receipt", "post_receipt", "unbilled"]
