"""A store's DOKU Lakuin balance: what buyers paid through the platform's DOKU account, less
Lakuin's fee, less what Lakuin already transferred. Money becomes withdrawable once the order
is delivered; until then a cancellation or refund could still take it back."""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import Sum

from apps.orders.models import Order

from .models import PlatformPayout

PENDING_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed", "shipped"}
AVAILABLE_STATUSES = {"delivered", "completed"}


def platform_orders(store):
    """Orders this store was paid for through the platform's account."""
    return Order.objects.filter(store=store, paid_to_store_account=False, payment_provider="doku")


def fee(amount):
    return (Decimal(amount) * settings.PLATFORM_DOKU_FEE_PERCENT / 100).quantize(Decimal(1),
        rounding=ROUND_HALF_UP)


def balance(store):
    orders = platform_orders(store)
    pending = orders.filter(status__in=PENDING_STATUSES).aggregate(total=Sum("total"))["total"] or 0
    earned = orders.filter(status__in=AVAILABLE_STATUSES).aggregate(total=Sum("total"))["total"] or 0
    payouts = PlatformPayout.objects.filter(store=store).exclude(status="rejected")
    paid_out = payouts.filter(status="paid").aggregate(total=Sum("amount"))["total"] or 0
    requested = payouts.filter(status="requested").aggregate(total=Sum("amount"))["total"] or 0
    net_earned = Decimal(earned) - fee(earned)
    return {"pending": Decimal(pending) - fee(pending), "earned": net_earned,
        "fee_percent": settings.PLATFORM_DOKU_FEE_PERCENT, "fees": fee(earned),
        "paid_out": Decimal(paid_out), "requested": Decimal(requested),
        "available": max(Decimal(0), net_earned - Decimal(paid_out) - Decimal(requested))}
