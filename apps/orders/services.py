from decimal import Decimal

from django.db import models
from django.utils import timezone

from .models import Coupon

ORDER_TRANSITIONS = {
    "pending": {"paid", "payment_failed", "cancelled"},
    "payment_failed": {"pending", "cancelled"},
    "paid": {"confirmed", "refund_requested", "refunded"},
    "confirmed": {"allocated", "refund_requested", "cancelled"},
    "allocated": {"picking", "cancelled"},
    "picking": {"packed"},
    "packed": {"shipped"},
    "shipped": {"delivered"},
    "delivered": {"completed", "refund_requested", "returned"},
    "completed": {"refund_requested", "returned"},
    "refund_requested": {"partially_refunded", "refunded"},
    "partially_refunded": {"refunded", "returned"},
    "returned": {"partially_refunded", "refunded"},
    "cancelled": set(), "refunded": set(),
}


class InvalidOrderTransition(ValueError):
    pass


def allowed_order_transitions(order):
    labels = dict(order.STATUS)
    return [(status, labels[status]) for status in ORDER_TRANSITIONS.get(order.status, set())]


def transition_order(order, new_status, *, note="", release_inventory=False):
    from apps.integrations.models import OutboxEvent

    from .models import OrderStatusEvent

    if new_status == order.status:
        return False
    if new_status not in ORDER_TRANSITIONS.get(order.status, set()):
        raise InvalidOrderTransition(f"Transisi {order.status} → {new_status} tidak diizinkan.")
    old_status = order.status
    if release_inventory or new_status == "cancelled":
        release_order_inventory(order)
    order.status = new_status
    order.save(update_fields=["status"])
    OrderStatusEvent.objects.create(order=order, status=new_status, note=note)
    OutboxEvent.objects.create(store=order.store, topic="order.status_changed",
        aggregate_id=str(order.id), payload={"order_id": str(order.id), "from": old_status,
            "status": new_status, "phone": order.phone})
    return True


def normalize_indonesian_phone(value):
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("62"):
        digits = "0" + digits[2:]
    if not digits.startswith("08") or not 4 <= len(digits) <= 15:
        raise ValueError("Gunakan nomor Indonesia yang valid, misalnya 081234567890.")
    return digits


def get_coupon(store, code, subtotal):
    if not code:
        return None
    now = timezone.now()
    coupon = Coupon.objects.filter(store=store, code__iexact=code.strip(), is_active=True).first()
    if not coupon or subtotal < coupon.minimum_spend:
        return None
    if coupon.starts_at and coupon.starts_at > now:
        return None
    if coupon.ends_at and coupon.ends_at < now:
        return None
    if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
        return None
    return coupon


def calculate_discount(coupon, subtotal, shipping_cost):
    if not coupon:
        return Decimal(0), shipping_cost
    if coupon.kind == "percent":
        discount = (subtotal * coupon.value / Decimal(100)).quantize(Decimal(1))
    elif coupon.kind == "fixed":
        discount = min(coupon.value, subtotal)
    else:
        discount, shipping_cost = Decimal(0), Decimal(0)
    return discount, shipping_cost


def release_order_inventory(order):
    from apps.catalog.models import ProductVariant

    from .models import InventoryAllocation

    for allocation in InventoryAllocation.objects.select_for_update().filter(
        order_item__order=order
    ).select_related("inventory_level"):
        level = allocation.inventory_level
        level.reserved = max(0, level.reserved - allocation.quantity)
        level.save(update_fields=["reserved", "updated_at"])
    for item in order.items.select_for_update().exclude(variant_id=None):
        ProductVariant.objects.filter(id=item.variant_id).update(stock=models.F("stock") + item.quantity)
