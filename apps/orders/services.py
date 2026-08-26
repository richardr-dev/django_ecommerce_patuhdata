from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Coupon

ORDER_TRANSITIONS = {
    "pending": {"paid", "payment_failed", "cancelled"},
    "payment_failed": {"cancelled"},
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


@transaction.atomic
def transition_order(order, new_status, *, note="", release_inventory=False, actor=None):
    from apps.integrations.models import OutboxEvent

    from .models import Order, OrderStatusEvent

    locked_order = Order.objects.select_for_update().get(pk=order.pk)
    if new_status == locked_order.status:
        return False
    if new_status not in ORDER_TRANSITIONS.get(locked_order.status, set()):
        raise InvalidOrderTransition(
            f"Transisi {locked_order.status} → {new_status} tidak diizinkan."
        )
    old_status = locked_order.status
    if release_inventory or new_status == "cancelled":
        from .inventory import release_order_inventory
        release_order_inventory(locked_order, actor=actor)
    if new_status == "packed":
        from .inventory import commit_order_inventory
        commit_order_inventory(locked_order, actor=actor)
    locked_order.status = new_status
    locked_order.save(update_fields=["status"])
    OrderStatusEvent.objects.create(order=locked_order, status=new_status, note=note)
    OutboxEvent.objects.create(store=locked_order.store, topic="order.status_changed",
        aggregate_id=str(locked_order.id), payload={"order_id": str(locked_order.id),
            "from": old_status, "status": new_status, "phone": locked_order.phone})
    order.status = new_status
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


def expire_pending_orders(*, now=None, limit=100):
    """Cancel expired unpaid orders in small, independently recoverable transactions."""
    from .models import Order

    now = now or timezone.now()
    order_ids = list(Order.objects.filter(status="pending", payment_expires_at__lte=now)
        .order_by("payment_expires_at").values_list("id", flat=True)[:limit])
    expired = 0
    for order_id in order_ids:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.status != "pending" or not order.payment_expires_at or order.payment_expires_at > now:
                continue
            if transition_order(order, "cancelled", note="Sesi pembayaran kedaluwarsa"):
                expired += 1
    return expired
