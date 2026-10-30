import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Coupon

logger = logging.getLogger("niagakit.orders")

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
    from .loyalty import apply_order_loyalty
    apply_order_loyalty(locked_order, new_status)
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


# Orders that never completed payment give their voucher use back.
COUPON_RELEASED_STATUSES = {"cancelled", "payment_failed"}


def coupon_uses_by_customer(coupon, *, user, phone):
    """How often this customer (signed-in account or WhatsApp number) already used the voucher."""
    from django.db.models import Q

    from .models import Order

    who = Q(phone=phone)
    if user is not None and user.is_authenticated:
        who |= Q(customer_user=user)
    return (Order.objects.filter(who, store_id=coupon.store_id, coupon_code__iexact=coupon.code)
        .exclude(status__in=COUPON_RELEASED_STATUSES).count())


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


# After this long past expiry an order is cancelled even if the gateway cannot be reached; a
# payment that still arrives later is kept as an unreconciled transaction for finance to refund.
GATEWAY_STATUS_GRACE = timedelta(hours=6)


def _gateway_status(order, now):
    """Ask the gateway whether an expired order was paid; None means cancel without asking."""
    from apps.integrations.http import ProviderError
    from apps.integrations.payments import get_payment_provider

    if not order.payment_reference:
        return None
    try:
        provider = get_payment_provider(order.payment_provider)
    except (ProviderError, ValueError):
        return None
    if not hasattr(provider, "fetch_status"):
        return None
    try:
        return provider.fetch_status(order)
    except ProviderError:
        if now - order.payment_expires_at >= GATEWAY_STATUS_GRACE:
            return None
        return "retry"


def expire_pending_orders(*, now=None, limit=100):
    """Cancel expired unpaid orders in small, independently recoverable transactions.

    Gateways with a status API are asked first (outside any lock), so a paid order whose
    notification was lost is marked paid instead of being cancelled.
    """
    from apps.integrations.reconciliation import confirm_payment

    from .models import Order

    now = now or timezone.now()
    orders = list(Order.objects.filter(status="pending", payment_expires_at__lte=now)
        .order_by("payment_expires_at")[:limit])
    expired = 0
    for candidate in orders:
        try:
            expired += _expire_one(candidate, now, confirm_payment)
        except Exception:  # one bad order must not keep every other reservation locked
            logger.exception("could not expire order %s", candidate.id)
    return expired


def _expire_one(candidate, now, confirm_payment):
    from .models import Order

    status = _gateway_status(candidate, now)
    if status == "retry" or getattr(status, "state", None) == "pending":
        return 0
    with transaction.atomic():
        order = Order.objects.select_for_update().get(id=candidate.id)
        if order.status != "pending" or not order.payment_expires_at or order.payment_expires_at > now:
            return 0
        if getattr(status, "state", None) == "paid":
            confirm_payment(order, provider=order.payment_provider,
                external_id=status.external_id, provider_payment_id=status.external_id,
                amount=status.amount, currency=status.currency, payload=status.payload or {},
                note="Pembayaran terverifikasi lewat cek status gateway")
            return 0
        return int(transition_order(order, "cancelled", note="Sesi pembayaran kedaluwarsa"))
