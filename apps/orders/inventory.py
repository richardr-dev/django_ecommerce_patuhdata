from django.db import transaction
from django.utils import timezone

from apps.catalog.models import ProductVariant

from .models import InventoryAllocation, InventoryMovement


def record_movement(*, store, kind, quantity, reason, order_item=None, inventory_level=None,
    variant=None, previous_on_hand, new_on_hand, previous_reserved, new_reserved, actor=None):
    return InventoryMovement.objects.create(store=store, order_item=order_item,
        inventory_level=inventory_level, variant=variant, kind=kind, quantity=quantity, reason=reason,
        actor=actor, previous_on_hand=previous_on_hand, new_on_hand=new_on_hand,
        previous_reserved=previous_reserved, new_reserved=new_reserved)


def reserve_variant(order_item, variant, quantity):
    previous_reserved = variant.reserved
    if variant.available_stock < quantity:
        raise ValueError("Stok varian tidak mencukupi.")
    variant.reserved += quantity
    variant.save(update_fields=["reserved"])
    record_movement(store=order_item.order.store, order_item=order_item, variant=variant,
        kind="reserve", quantity=quantity, reason="checkout", previous_on_hand=variant.stock,
        new_on_hand=variant.stock, previous_reserved=previous_reserved, new_reserved=variant.reserved)


def record_level_reservation(order_item, level, quantity, previous_reserved):
    record_movement(store=order_item.order.store, order_item=order_item, inventory_level=level,
        kind="reserve", quantity=quantity, reason="checkout", previous_on_hand=level.on_hand,
        new_on_hand=level.on_hand, previous_reserved=previous_reserved, new_reserved=level.reserved)


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
    for item in order.items.select_for_update().exclude(variant_id=None):
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
    for item in order.items.select_for_update().exclude(variant_id=None):
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
