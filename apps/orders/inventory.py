from django.db import transaction
from django.utils import timezone

from apps.catalog.models import ProductVariant

from .models import InventoryAllocation, InventoryMovement


def record_movement(*, store, kind, quantity, reason, order_item=None, inventory_level=None,
    variant=None, previous_on_hand, new_on_hand, previous_reserved, new_reserved, actor=None,
    source_type="", source_id="", unit_cost=None):
    return InventoryMovement.objects.create(store=store, order_item=order_item,
        inventory_level=inventory_level, variant=variant, kind=kind, quantity=quantity, reason=reason,
        actor=actor, previous_on_hand=previous_on_hand, new_on_hand=new_on_hand,
        previous_reserved=previous_reserved, new_reserved=new_reserved,
        source_type=source_type, source_id=str(source_id), unit_cost=unit_cost)


def reserve_variant(order_item, variant, quantity):
    previous_reserved = variant.reserved
    if variant.available_stock < quantity:
        raise ValueError("Stok varian tidak mencukupi.")
    variant.reserved += quantity
    variant.save(update_fields=["reserved"])
    record_movement(store=order_item.order.store, order_item=order_item, variant=variant,
        kind="reserve", quantity=quantity, reason="checkout", previous_on_hand=variant.stock,
        new_on_hand=variant.stock, previous_reserved=previous_reserved, new_reserved=variant.reserved)


def reserve_from_levels(order_item, levels, quantity, *, reason="checkout"):
    """Reserve across locked warehouse levels in priority order. Returns the quantity that
    could not be reserved (0 when fully covered)."""
    remaining = quantity
    for level in levels:
        allocated = min(remaining, level.available)
        if not allocated:
            continue
        previous_reserved = level.reserved
        level.reserved += allocated
        level.save(update_fields=["reserved", "updated_at"])
        InventoryAllocation.objects.create(order_item=order_item, inventory_level=level,
            quantity=allocated)
        record_movement(store=order_item.order.store, order_item=order_item, inventory_level=level,
            kind="reserve", quantity=allocated, reason=reason, previous_on_hand=level.on_hand,
            new_on_hand=level.on_hand, previous_reserved=previous_reserved,
            new_reserved=level.reserved)
        remaining -= allocated
        if not remaining:
            break
    return remaining


@transaction.atomic
def release_order_inventory(order, *, actor=None, reason="order_cancelled"):
    allocations = InventoryAllocation.objects.select_for_update().filter(
        order_item__order=order, committed_at__isnull=True).select_related("inventory_level", "order_item")
    for allocation in allocations:
        if InventoryMovement.objects.filter(order_item=allocation.order_item,
                inventory_level=allocation.inventory_level, kind="release").exists():
            continue
        level = allocation.inventory_level
        previous_reserved = level.reserved
        level.reserved = max(0, level.reserved - allocation.quantity)
        level.save(update_fields=["reserved", "updated_at"])
        record_movement(store=order.store, order_item=allocation.order_item, inventory_level=level,
            kind="release", quantity=allocation.quantity, reason=reason, actor=actor,
            previous_on_hand=level.on_hand, new_on_hand=level.on_hand,
            previous_reserved=previous_reserved, new_reserved=level.reserved)
    # Pre-order items were never reserved: they use a daily quota, not stock.
    for item in order.items.select_for_update().exclude(variant_id=None).exclude(is_preorder=True):
        variant = ProductVariant.objects.select_for_update().get(id=item.variant_id)
        if InventoryMovement.objects.filter(order_item=item, variant=variant,
                kind__in={"release", "commit"}).exists():
            continue
        previous_reserved = variant.reserved
        variant.reserved = max(0, variant.reserved - item.quantity)
        variant.save(update_fields=["reserved"])
        record_movement(store=order.store, order_item=item, variant=variant, kind="release",
            quantity=item.quantity, reason=reason, actor=actor, previous_on_hand=variant.stock,
            new_on_hand=variant.stock, previous_reserved=previous_reserved,
            new_reserved=variant.reserved)


@transaction.atomic
def commit_order_inventory(order, *, actor=None):
    allocations = InventoryAllocation.objects.select_for_update().filter(
        order_item__order=order, committed_at__isnull=True).select_related("inventory_level", "order_item")
    for allocation in allocations:
        level = allocation.inventory_level
        if level.reserved < allocation.quantity or level.on_hand < allocation.quantity:
            raise ValueError("Reservasi inventory tidak konsisten dan tidak dapat dikomit.")
        previous_on_hand, previous_reserved = level.on_hand, level.reserved
        level.on_hand -= allocation.quantity
        level.reserved -= allocation.quantity
        level.save(update_fields=["on_hand", "reserved", "updated_at"])
        allocation.committed_at = timezone.now()
        allocation.save(update_fields=["committed_at"])
        record_movement(store=order.store, order_item=allocation.order_item, inventory_level=level,
            kind="commit", quantity=allocation.quantity, reason="order_packed", actor=actor,
            previous_on_hand=previous_on_hand, new_on_hand=level.on_hand,
            previous_reserved=previous_reserved, new_reserved=level.reserved)
    # Pre-order items were never reserved: they use a daily quota, not stock.
    for item in order.items.select_for_update().exclude(variant_id=None).exclude(is_preorder=True):
        variant = ProductVariant.objects.select_for_update().get(id=item.variant_id)
        if InventoryMovement.objects.filter(order_item=item, variant=variant, kind="commit").exists():
            continue
        if variant.reserved < item.quantity or variant.stock < item.quantity:
            raise ValueError("Reservasi varian tidak konsisten dan tidak dapat dikomit.")
        previous_on_hand, previous_reserved = variant.stock, variant.reserved
        variant.stock -= item.quantity
        variant.reserved -= item.quantity
        variant.save(update_fields=["stock", "reserved"])
        record_movement(store=order.store, order_item=item, variant=variant, kind="commit",
            quantity=item.quantity, reason="order_packed", actor=actor,
            previous_on_hand=previous_on_hand, new_on_hand=variant.stock,
            previous_reserved=previous_reserved, new_reserved=variant.reserved)


def stock_shortages(store, wants):
    """``wants`` is ``[(product, quantity)]``. Returns ``[(product, wanted, available)]`` for the
    stock-tracked products (those with an inventory level) the shelf cannot cover. Products with
    no inventory level (services, digital goods) are not tracked and never short."""
    from apps.catalog.models import InventoryLevel
    need = {}
    for product, quantity in wants:
        held = need.setdefault(product.pk, [product, 0])
        held[1] += int(quantity)
    short = []
    for product, quantity in need.values():
        levels = list(InventoryLevel.objects.filter(product=product, warehouse__store=store,
            warehouse__is_active=True))
        if levels and sum(level.available for level in levels) < quantity:
            short.append((product, quantity, sum(level.available for level in levels)))
    return short


def deduct_invoice_stock(order, *, allow_negative=False, actor=None):
    """Reserve then immediately commit stock for all items on a manual paid invoice.
    Net effect: on_hand decreases by quantity sold.
    Silently skips items without inventory levels (services, digital products).
    Returns list of (sku, wanted, available) shortages for display. With ``allow_negative`` a
    warehouse-tracked product gives what it has and the rest becomes stok minus (``deficits``)."""
    from apps.catalog.models import InventoryLevel
    from apps.catalog.models import ProductVariant as _PV
    shortages = []
    with transaction.atomic():
        for item in order.items.select_related("product").all():
            if not item.product_id:
                continue
            qty = int(item.quantity)
            if item.variant_id:
                try:
                    variant = _PV.objects.select_for_update().get(id=item.variant_id)
                    if variant.available_stock >= qty:
                        reserve_variant(item, variant, qty)
                    else:
                        shortages.append((item.sku, qty, variant.available_stock))
                        continue
                except _PV.DoesNotExist:
                    continue
            else:
                levels = list(InventoryLevel.objects.select_for_update().filter(
                    product=item.product,
                    warehouse__store=order.store,
                    warehouse__is_active=True,
                ).order_by("warehouse__priority", "warehouse_id"))
                if not levels:
                    continue  # product not tracked in inventory
                available = sum(level.available for level in levels)
                if available < qty:
                    if not allow_negative or item.product.cost_price is None:
                        shortages.append((item.sku, qty, available))
                        continue
                    from .deficits import sell_short
                    if available > 0:
                        reserve_from_levels(item, levels, available, reason="manual_invoice")
                    sell_short(item, levels[0], qty - available, actor=actor)
                    continue
                reserve_from_levels(item, levels, qty, reason="manual_invoice")
        commit_order_inventory(order)
    return shortages


@transaction.atomic
def return_order_inventory(order, *, actor=None, reason="pesanan_dibatalkan"):
    """Give an order's stock back: still-reserved stock is released and stock already taken out
    is added back to the shelf. Safe to call again; each item returns once."""
    from .deficits import release as release_deficits
    release_deficits(order, actor=actor, reason=reason)
    release_order_inventory(order, actor=actor, reason=reason)
    allocations = InventoryAllocation.objects.select_for_update().filter(
        order_item__order=order, committed_at__isnull=False).select_related(
        "inventory_level", "order_item")
    for allocation in allocations:
        if InventoryMovement.objects.filter(order_item=allocation.order_item,
                inventory_level=allocation.inventory_level, kind="return").exists():
            continue
        level = allocation.inventory_level
        previous_on_hand = level.on_hand
        level.on_hand += allocation.quantity
        level.save(update_fields=["on_hand", "updated_at"])
        record_movement(store=order.store, order_item=allocation.order_item,
            inventory_level=level, kind="return", quantity=allocation.quantity, reason=reason,
            actor=actor, previous_on_hand=previous_on_hand, new_on_hand=level.on_hand,
            previous_reserved=level.reserved, new_reserved=level.reserved)
    for item in order.items.select_for_update().exclude(variant_id=None).exclude(is_preorder=True):
        variant = ProductVariant.objects.select_for_update().get(id=item.variant_id)
        committed = InventoryMovement.objects.filter(order_item=item, variant=variant,
            kind="commit").exists()
        if not committed or InventoryMovement.objects.filter(order_item=item, variant=variant,
                kind="return").exists():
            continue
        previous_on_hand = variant.stock
        variant.stock += item.quantity
        variant.save(update_fields=["stock"])
        record_movement(store=order.store, order_item=item, variant=variant, kind="return",
            quantity=item.quantity, reason=reason, actor=actor, previous_on_hand=previous_on_hand,
            new_on_hand=variant.stock, previous_reserved=variant.reserved,
            new_reserved=variant.reserved)
