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
        # Without a default plan configured, a lapsed store keeps what it has but grows no further.
        return default_plan() or Plan(code="lapsed", name="Langganan berakhir", max_products=0)
    return plan


def product_limit_error(store, active_after):
    """Message when `active_after` active products would exceed the plan, else ""."""
    plan = effective_plan(store)
    if plan is None or plan.max_products is None or active_after <= plan.max_products:
        return ""
    return (f"Paket {plan.name} memuat maksimal {plan.max_products} produk aktif. "
        "Upgrade paket di menu Langganan untuk menambah produk.")
