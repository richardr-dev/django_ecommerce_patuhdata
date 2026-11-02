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
    end = subscription.current_period_end
    if plan.is_paid and end and end <= timezone.now() < end + timedelta(
            days=settings.RENEWAL_GRACE_DAYS) and subscription.auto_renew and (
            store.subscription_invoices.filter(status="paid").exists()):
        # A paying seller a few days late with the renewal keeps everything meanwhile.
        return plan
    if plan.is_paid and not (end and end > timezone.now()):
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
    from .renewals import pending_renewal

    end = subscription.current_period_end
    trial = not store.subscription_invoices.filter(status="paid").exists()
    days_left = (end - timezone.now()).days if end else -1
    ended = not end or end <= timezone.now()
    grace = ended and effective_plan(store).code == subscription.plan.code
    return {"plan": subscription.plan, "ends_at": end, "trial": trial, "grace": grace,
        "lapsed": ended and not grace, "days_left": max(days_left, 0),
        "ending_soon": end is not None and 0 <= days_left <= 3,
        "auto_renew": subscription.auto_renew, "renewal": pending_renewal(store)}


def _limit_error(store, field, count_after, what):
    plan = effective_plan(store)
    limit = getattr(plan, field, None) if plan else None
    if limit is None or count_after <= limit:
        return ""
    if plan.code == "lapsed":
        return (f"Masa aktif paket toko ini sudah berakhir, jadi {what} belum bisa ditambah. "
            "Pilih paket di menu Langganan; produk yang sudah ada tetap tampil.")
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


def plan_amount(plan, months, extra_outlets=0):
    """What a period costs: the plan plus its extra outlets (a year is billed as ten months,
    like the plan's own yearly price)."""
    per_outlet = (plan.extra_outlet_price or 0) * (10 if months == 12 else months)
    return plan.price_for(months) + per_outlet * extra_outlets


def allowed_outlets(store):
    """Outlets the store may run now, or None for no limit."""
    plan = effective_plan(store)
    if plan is None or plan.included_outlets is None:
        return None
    subscription = getattr(store, "subscription", None)
    extra = subscription.extra_outlets if subscription and plan.extra_outlet_price else 0
    return plan.included_outlets + extra


def outlet_limit_error(store, count_after):
    allowed = allowed_outlets(store)
    if allowed is None or count_after <= allowed:
        return ""
    plan = effective_plan(store)
    if plan.extra_outlet_price:
        return (f"Paket {plan.name} Anda memuat {allowed} outlet. Tambah outlet di menu Langganan "
            f"(Rp{plan.extra_outlet_price:,.0f} per outlet per bulan).".replace(",", "."))
    return (f"Paket {plan.name} hanya untuk 1 outlet. Multi-outlet tersedia di paket Pro "
        "(per outlet) atau Custom; upgrade di menu Langganan.")
