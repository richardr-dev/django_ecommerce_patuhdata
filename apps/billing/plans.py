from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Plan, Subscription

# A paid period; 12 months is billed at the plan's yearly price.
PERIOD_DAYS = {1: 30, 12: 365}


def default_plan():
    return Plan.objects.filter(is_default=True).first()


def effective_plan(store):
    """The plan whose limits apply now, or None for an operator-managed store without a
    subscription (no limits). A lapsed paid plan falls back to the default plan."""
    try:
        subscription = store.subscription
    except Subscription.DoesNotExist:
        return None
    plan = subscription.plan
    if plan.is_paid and not (subscription.current_period_end and
            subscription.current_period_end > timezone.now()):
        # A lapsed store falls back to a free default plan if there is one; otherwise it stays
        # online with what it has but grows no further until the seller pays.
        fallback = default_plan()
        if fallback and not fallback.is_paid:
            return fallback
        return Plan(code="lapsed", name="Masa aktif berakhir", max_products=0, max_channels=0)
    return plan


def trial_end_for(plan):
    """When a new store's free trial of a paid default plan ends, or None for a free plan."""
    return timezone.now() + timedelta(days=settings.TRIAL_DAYS) if plan.is_paid else None


def subscription_status(store):
    """For the dashboard and billing page: trial, active, ending soon, or lapsed."""
    subscription = getattr(store, "subscription", None)
    if subscription is None or not subscription.plan.is_paid:
        return None
    end = subscription.current_period_end
    trial = not store.subscription_invoices.filter(status="paid").exists()
    days_left = (end - timezone.now()).days if end else -1
    return {"plan": subscription.plan, "ends_at": end, "trial": trial,
        "lapsed": not end or end <= timezone.now(), "days_left": max(days_left, 0),
        "ending_soon": end is not None and 0 <= days_left <= 3}


def _limit_error(store, field, count_after, what):
    plan = effective_plan(store)
    limit = getattr(plan, field, None) if plan else None
    if limit is None or count_after <= limit:
        return ""
    return (f"Paket {plan.name} memuat maksimal {limit} {what}. "
        "Upgrade paket di menu Langganan untuk menambah.")


def product_limit_error(store, active_after):
    """Message when `active_after` active products would exceed the plan, else ""."""
    return _limit_error(store, "max_products", active_after, "produk aktif")


def channel_limit_error(store, count_after):
    return _limit_error(store, "max_channels", count_after, "toko marketplace")


def can_reconcile_payouts(store):
    plan = effective_plan(store)
    return plan is None or plan.allow_payout_reconciliation
