"""Stock that moves for reasons other than a customer order: goods received from a supplier,
transfers, counts (stok opname), and goods sent to or taken back from a consignment mitra.

Every change goes through here and leaves an ``InventoryMovement``, so the stock of an item is
always explained by its movements (see ``stock_audit``). Order-driven movements live in
``inventory.py``."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Sum

from apps.catalog.models import InventoryLevel, ProductVariant

from .inventory import record_movement
from .models import InventoryMovement


class StockError(ValueError):
    pass


def _levels(product, *, lock=True):
    queryset = InventoryLevel.objects.filter(product=product, warehouse__store=product.store,
        warehouse__is_active=True).order_by("warehouse__priority", "warehouse_id")
    return list(queryset.select_for_update() if lock else queryset)


def default_warehouse(store):
    warehouse = store.warehouses.filter(is_active=True).order_by("priority", "id").first()
    if not warehouse:
        raise StockError("Belum ada gudang aktif. Buat gudang terlebih dahulu.")
    return warehouse


def on_shelf(product, variant=None):
    """Units physically in the store's own warehouses (reserved ones included)."""
    if variant:
        return variant.stock
    return sum(level.on_hand for level in _levels(product, lock=False))


def with_mitra(product, variant=None):
    from apps.settlements.models import ConsignmentStock
    rows = ConsignmentStock.objects.filter(product=product, variant=variant)
    return rows.aggregate(total=Sum("quantity", default=0))["total"]


@transaction.atomic
def stock_out(*, store, product, variant=None, quantity, kind, reason, source=("", ""),
        unit_cost=None, actor=None):
    """Take ``quantity`` units off the shelf (never reserved ones). Returns the units taken per
    location as ``[(level or variant, quantity)]``."""
    if quantity <= 0:
        raise StockError("Jumlah harus lebih dari nol.")
    taken = []
    if variant:
        variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        free = variant.stock - variant.reserved
        if free < quantity:
            raise StockError(f"Stok {variant.sku or variant.name} kurang: butuh {quantity}, "
                f"tersedia {free}.")
        before = variant.stock
        variant.stock -= quantity
        variant.save(update_fields=["stock"])
        record_movement(store=store, kind=kind, quantity=quantity, reason=reason, variant=variant,
            previous_on_hand=before, new_on_hand=variant.stock, previous_reserved=variant.reserved,
            new_reserved=variant.reserved, actor=actor, source_type=source[0],
            source_id=source[1], unit_cost=unit_cost)
        return [(variant, quantity)]
    levels = _levels(product)
    free = sum(level.on_hand - level.reserved for level in levels)
    if free < quantity:
        raise StockError(f"Stok {product.sku or product.name} kurang: butuh {quantity}, "
            f"tersedia {free}.")
    remaining = quantity
    for level in levels:
        amount = min(remaining, level.on_hand - level.reserved)
        if amount <= 0:
            continue
        before = level.on_hand
        level.on_hand -= amount
        level.save(update_fields=["on_hand", "updated_at"])
        record_movement(store=store, kind=kind, quantity=amount, reason=reason,
            inventory_level=level, previous_on_hand=before, new_on_hand=level.on_hand,
            previous_reserved=level.reserved, new_reserved=level.reserved, actor=actor,
            source_type=source[0], source_id=source[1], unit_cost=unit_cost)
        taken.append((level, amount))
        remaining -= amount
        if not remaining:
            break
    return taken


@transaction.atomic
def stock_in(*, store, product, variant=None, quantity, kind, reason, source=("", ""),
        warehouse=None, unit_cost=None, actor=None):
    """Put ``quantity`` units on the shelf: of the variant, or of the product in ``warehouse``
    (the first active warehouse when not given)."""
    if quantity <= 0:
        raise StockError("Jumlah harus lebih dari nol.")
    if variant:
        variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        before = variant.stock
        variant.stock += quantity
        variant.save(update_fields=["stock"])
        return record_movement(store=store, kind=kind, quantity=quantity, reason=reason,
            variant=variant, previous_on_hand=before, new_on_hand=variant.stock,
            previous_reserved=variant.reserved, new_reserved=variant.reserved, actor=actor,
            source_type=source[0], source_id=source[1], unit_cost=unit_cost)
    warehouse = warehouse or default_warehouse(store)
    level, _ = InventoryLevel.objects.select_for_update().get_or_create(
        warehouse=warehouse, product=product)
    before = level.on_hand
    level.on_hand += quantity
    level.save(update_fields=["on_hand", "updated_at"])
    return record_movement(store=store, kind=kind, quantity=quantity, reason=reason,
        inventory_level=level, previous_on_hand=before, new_on_hand=level.on_hand,
        previous_reserved=level.reserved, new_reserved=level.reserved, actor=actor,
        source_type=source[0], source_id=source[1], unit_cost=unit_cost)


def moving_average(old_quantity, old_cost, new_quantity, new_cost):
    """Weighted-average unit cost after receiving ``new_quantity`` at ``new_cost``."""
    if old_cost is None or old_quantity <= 0:
        return Decimal(new_cost).quantize(Decimal("0.01"))
    total = old_quantity * Decimal(old_cost) + new_quantity * Decimal(new_cost)
    return (total / (old_quantity + new_quantity)).quantize(Decimal("0.01"), ROUND_HALF_UP)


@transaction.atomic
def receive_goods(*, store, product, variant=None, quantity, unit_cost, warehouse=None,
        reason="Barang masuk dari supplier", source=("", ""), actor=None):
    """Goods received from a supplier: stock goes up and the item's cost price becomes the
    moving average of what is held (shelf and mitra) and what just arrived."""
    holder = variant or product
    held = on_shelf(product, variant) + with_mitra(product, variant)
    movement = stock_in(store=store, product=product, variant=variant, quantity=quantity,
        kind="receipt", reason=reason, source=source, warehouse=warehouse, unit_cost=unit_cost,
        actor=actor)
    holder.cost_price = moving_average(max(held, 0), holder.cost_price, quantity, unit_cost)
    holder.save(update_fields=["cost_price"])
    return movement


@transaction.atomic
def transfer_stock(*, store, product, quantity, source, target, reason="Transfer gudang",
        actor=None):
    """Move product stock between two of the store's warehouses."""
    if source.pk == target.pk:
        raise StockError("Gudang asal dan tujuan sama.")
    if source.store_id != store.id or target.store_id != store.id:
        raise StockError("Gudang bukan milik toko ini.")
    level = InventoryLevel.objects.select_for_update().filter(warehouse=source,
        product=product).first()
    if not level or level.on_hand - level.reserved < quantity:
        raise StockError(f"Stok di {source.name} kurang untuk transfer {quantity} unit.")
    before = level.on_hand
    level.on_hand -= quantity
    level.save(update_fields=["on_hand", "updated_at"])
    record_movement(store=store, kind="transfer_out", quantity=quantity, reason=reason,
        inventory_level=level, previous_on_hand=before, new_on_hand=level.on_hand,
        previous_reserved=level.reserved, new_reserved=level.reserved, actor=actor,
        source_type="transfer", source_id=target.pk)
    stock_in(store=store, product=product, quantity=quantity, kind="transfer_in", reason=reason,
        warehouse=target, source=("transfer", source.pk), actor=actor)


@transaction.atomic
def count_stock(*, store, product, variant=None, warehouse=None, counted, reason="Stok opname",
        actor=None):
    """Stok opname: set the real counted quantity. The difference is recorded as a movement
    with the reason, never silently overwritten."""
    if counted < 0:
        raise StockError("Hasil hitung tidak boleh negatif.")
    if variant:
        variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        current, reserved, target = variant.stock, variant.reserved, variant
    else:
        warehouse = warehouse or default_warehouse(store)
        target, _ = InventoryLevel.objects.select_for_update().get_or_create(
            warehouse=warehouse, product=product)
        current, reserved = target.on_hand, target.reserved
    if counted < reserved:
        raise StockError(f"Hasil hitung {counted} lebih kecil dari stok yang sudah dipesan "
            f"({reserved}).")
    if counted == current:
        return None
    if variant:
        variant.stock = counted
        variant.save(update_fields=["stock"])
    else:
        target.on_hand = counted
        target.save(update_fields=["on_hand", "updated_at"])
    return record_movement(store=store, kind="opname", quantity=abs(counted - current),
        reason=reason, variant=variant, inventory_level=None if variant else target,
        previous_on_hand=current, new_on_hand=counted, previous_reserved=reserved,
        new_reserved=reserved, actor=actor, source_type="opname")


def stock_audit(store):
    """Compare each stock figure with what its movements add up to. Returns
    ``[(item, recorded, explained)]`` for the ones that differ; an empty list means every unit
    is accounted for."""
    drift = []
    delta = Sum(F("new_on_hand") - F("previous_on_hand"))
    for level in InventoryLevel.objects.filter(warehouse__store=store).select_related(
            "product", "warehouse"):
        explained = InventoryMovement.objects.filter(inventory_level=level).aggregate(
            total=delta)["total"] or 0
        if explained != level.on_hand:
            drift.append((level, level.on_hand, explained))
    for variant in ProductVariant.objects.filter(product__store=store).select_related("product"):
        explained = InventoryMovement.objects.filter(variant=variant).aggregate(
            total=delta)["total"] or 0
        if explained != variant.stock:
            drift.append((variant, variant.stock, explained))
    return drift


@transaction.atomic
def record_opening_balances(store, *, actor=None):
    """Make every stock figure explainable by adding an opening movement for what its history
    does not account for. Run once when adopting the audit. Returns how many were added."""
    added = 0
    for item, recorded, explained in stock_audit(store):
        gap = recorded - explained
        is_variant = isinstance(item, ProductVariant)
        record_movement(store=store, kind="opname", quantity=abs(gap),
            reason="Saldo awal (stok sebelum pencatatan pergerakan)",
            variant=item if is_variant else None, inventory_level=None if is_variant else item,
            previous_on_hand=max(explained, 0), new_on_hand=recorded,
            previous_reserved=item.reserved, new_reserved=item.reserved, actor=actor,
            source_type="opening")
        added += 1
    return added
