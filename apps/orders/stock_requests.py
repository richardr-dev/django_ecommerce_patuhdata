"""Permintaan stok antar gudang / outlet (see ``models.StockRequest``).

request → approve (a different person) → send (stock leaves the source, in transit) → receive (stock
joins the target). A shortfall on receipt is a loss in transit: Selisih persediaan / Persediaan at the
item's cost. Free stock only is sent (reserved units are not); a product without variants only."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import InventoryLevel

from .models import StockRequest, StockRequestLine
from .stock import StockError, stock_in


class RequestError(ValueError):
    pass


def next_number(store, day):
    prefix = f"TR/{day:%Y}/"
    last = StockRequest.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('/', 1)[1]) + 1) if last else 1:04d}"


@transaction.atomic
def create_request(store, *, from_warehouse, to_warehouse, lines, notes="", actor=None):
    """``lines``: dicts with sku, qty."""
    from apps.accounting.payables import _find_goods
    if from_warehouse.pk == to_warehouse.pk:
        raise RequestError("Gudang asal dan tujuan sama.")
    if from_warehouse.store_id != store.pk or to_warehouse.store_id != store.pk:
        raise RequestError("Gudang bukan milik perusahaan ini.")
    picked = {}
    for line in lines:
        product, variant = _find_goods(store, line.get("sku", ""))
        if not product:
            raise RequestError(f"SKU {line.get('sku') or '(kosong)'} tidak ada di katalog.")
        if variant:
            raise RequestError(f"{product.name}: permintaan barang bervarian belum didukung.")
        qty = line["qty"]
        if qty != int(qty) or qty <= 0:
            raise RequestError(f"Jumlah {product.name} harus bilangan bulat lebih dari nol.")
        picked[product.pk] = (product, picked.get(product.pk, (product, 0))[1] + int(qty))
    if not picked:
        raise RequestError("Tambahkan minimal satu barang.")
    request = StockRequest.objects.create(store=store, number=next_number(store, timezone.localdate()),
        from_warehouse=from_warehouse, to_warehouse=to_warehouse, notes=notes[:300], requested_by=actor)
    StockRequestLine.objects.bulk_create(StockRequestLine(request=request, product=product, quantity=qty)
        for product, qty in picked.values())
    return request


def _locked(request, *, status):
    request = StockRequest.objects.select_for_update().select_related("store", "from_warehouse", "to_warehouse").get(pk=request.pk)
    if request.status not in (status if isinstance(status, tuple) else (status,)):
        raise RequestError(f"Permintaan ini sudah {request.get_status_display().lower()}.")
    return request


@transaction.atomic
def approve(request, *, actor, reject=False):
    from apps.shops import permissions
    request = _locked(request, status=StockRequest.REQUESTED)
    store = request.store
    if not permissions.can(actor, store, "stock.request_approve"):
        raise RequestError("Anda tidak berhak menyetujui permintaan stok.")
    if not reject and permissions.maker_may_not_check(store, actor, request.requested_by, "stock.request_approve"):
        raise RequestError("Permintaan yang Anda buat tidak boleh Anda setujui sendiri. Minta admin atau manajer lain.")
    request.status = StockRequest.REJECTED if reject else StockRequest.APPROVED
    request.approved_by = actor
    request.save(update_fields=["status", "approved_by"])
    return request


@transaction.atomic
def cancel(request):
    request = _locked(request, status=(StockRequest.REQUESTED, StockRequest.APPROVED))
    request.status = StockRequest.CANCELLED
    request.save(update_fields=["status"])
    return request


@transaction.atomic
def send(request, *, quantities, actor=None):
    """``quantities`` maps a line id to the units dispatched (at most requested, at most free stock)."""
    request = _locked(request, status=StockRequest.APPROVED)
    sent_any = False
    for line in request.lines.select_related("product"):
        qty = int(quantities.get(line.pk, 0) or 0)
        if qty < 0 or qty > line.quantity:
            raise RequestError(f"{line.product.name}: kirim 0 sampai {line.quantity}.")
        if not qty:
            continue
        level = InventoryLevel.objects.select_for_update().filter(warehouse=request.from_warehouse, product=line.product).first()
        free = (level.on_hand - level.reserved) if level else 0
        if qty > free:
            raise RequestError(f"{line.product.name}: stok bebas di {request.from_warehouse.name} hanya {free}.")
        try:
            _out(request, line, level, qty, actor)
        except StockError as error:
            raise RequestError(str(error)) from error
        line.sent_quantity, line.unit_cost = qty, line.product.cost_price
        line.save(update_fields=["sent_quantity", "unit_cost"])
        sent_any = True
    if not sent_any:
        raise RequestError("Isi jumlah yang dikirim untuk minimal satu barang.")
    request.status, request.sent_by, request.sent_at = StockRequest.SENT, actor, timezone.now()
    request.save(update_fields=["status", "sent_by", "sent_at"])
    return request


def _out(request, line, level, qty, actor):
    from .inventory import record_movement
    before = level.on_hand
    level.on_hand -= qty
    level.save(update_fields=["on_hand", "updated_at"])
    record_movement(store=request.store, kind="transfer_out", quantity=qty, reason=f"Kirim {request.number} ke {request.to_warehouse.name}"[:200],
        inventory_level=level, previous_on_hand=before, new_on_hand=level.on_hand, previous_reserved=level.reserved,
        new_reserved=level.reserved, actor=actor, source_type="stock_request", source_id=request.pk, unit_cost=line.product.cost_price)


@transaction.atomic
def receive(request, *, quantities, actor=None):
    """``quantities`` maps a line id to the units counted in. Missing units are written off as a loss
    in transit at cost (an item with no cost price blocks it: nothing is invented)."""
    from apps.accounting.ledger import INVENTORY, STOCK_DIFFERENCE, LedgerError, post_entry
    request = _locked(request, status=StockRequest.SENT)
    loss = Decimal(0)
    for line in request.lines.select_related("product"):
        if not line.sent_quantity:
            continue
        qty = int(quantities.get(line.pk, line.sent_quantity) or 0)
        if qty < 0 or qty > line.sent_quantity:
            raise RequestError(f"{line.product.name}: diterima 0 sampai {line.sent_quantity}.")
        missing = line.sent_quantity - qty
        if missing:
            if line.unit_cost is None:
                raise RequestError(f"{line.product.name} belum punya harga pokok, jadi kekurangannya tidak bisa dinilai.")
            loss += missing * line.unit_cost
        if qty:
            stock_in(store=request.store, product=line.product, quantity=qty, kind="transfer_in",
                reason=f"Terima {request.number} dari {request.from_warehouse.name}"[:200], warehouse=request.to_warehouse,
                source=("stock_request", str(request.pk)), unit_cost=line.unit_cost, actor=actor)
        line.received_quantity = qty
        line.save(update_fields=["received_quantity"])
    if loss:
        try:
            post_entry(request.store, date=timezone.localdate(), strict=True, actor=actor, source=("stock_request", request.pk),
                memo=f"Kekurangan kiriman {request.number}", amounts={STOCK_DIFFERENCE: loss, INVENTORY: -loss})
        except LedgerError as error:
            raise RequestError(str(error)) from error
    request.status, request.received_by, request.received_at = StockRequest.RECEIVED, actor, timezone.now()
    request.save(update_fields=["status", "received_by", "received_at"])
    return request


def in_transit_value(store):
    """Cost of units sent and not yet received (part of Persediaan, on no shelf)."""
    total = Decimal(0)
    for line in StockRequestLine.objects.filter(request__store=store, request__status=StockRequest.SENT):
        total += line.in_transit * (line.unit_cost or Decimal(0))
    return total


__all__ = ["RequestError", "approve", "cancel", "create_request", "in_transit_value", "receive", "send"]
