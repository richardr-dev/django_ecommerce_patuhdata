"""The partner edits one company's subscription: plan, paid-until date, auto-renew, billing period and extra
outlets, or takes the company off any plan (managed by the platform). Every change is written to the platform log."""
import datetime

from django.db import transaction
from django.utils import timezone

from apps.shops import platform_log

from .models import Plan, Subscription


class SubscriptionError(Exception):
    """A refusal the page shows as a message."""


def snapshot(store):
    sub = getattr(store, "subscription", None)
    if sub is None:
        return {"plan": "(tanpa paket)", "berlaku_sampai": "", "perpanjang_otomatis": "", "periode_tagihan": "",
            "outlet_tambahan": ""}
    return {"plan": f"{sub.plan.name} ({sub.plan.code})",
        "berlaku_sampai": sub.current_period_end.date().isoformat() if sub.current_period_end else "",
        "perpanjang_otomatis": "ya" if sub.auto_renew else "tidak",
        "periode_tagihan": "12 bulan" if sub.renew_months == 12 else "1 bulan",
        "outlet_tambahan": sub.extra_outlets}


@transaction.atomic
def update_subscription(store, *, plan_code, period_end="", auto_renew=True, renew_months=1, extra_outlets=0,
        actor=None):
    """Set a company's plan and dates. An empty ``plan_code`` removes the subscription (no plan limits)."""
    from apps.shops.trial import follow_paid_period

    before = snapshot(store)
    sub = getattr(store, "subscription", None)
    if not plan_code:
        if sub is not None:
            sub.delete()
        platform_log.log(actor, "subscription.removed", "Subscription", store.name,
            platform_log.diff(before, snapshot(Store_fresh(store))), store=store)
        return None
    plan = Plan.objects.filter(code=plan_code).first()
    if plan is None:
        raise SubscriptionError("Paket tidak ditemukan.")
    if renew_months not in (1, 12):
        raise SubscriptionError("Periode tagihan: 1 atau 12 bulan.")
    try:
        extra_outlets = int(extra_outlets or 0)
    except (TypeError, ValueError):
        raise SubscriptionError("Outlet tambahan harus angka.") from None
    if extra_outlets < 0:
        raise SubscriptionError("Outlet tambahan tidak boleh negatif.")
    if extra_outlets and not plan.extra_outlet_price:
        raise SubscriptionError(f"Paket {plan.name} tidak menjual outlet tambahan.")
    end = None
    if period_end:
        try:
            day = datetime.date.fromisoformat(period_end)
        except ValueError:
            raise SubscriptionError("Tanggal berlaku sampai tidak valid.") from None
        end = timezone.make_aware(datetime.datetime.combine(day, datetime.time(23, 59, 59)))
    if plan.is_paid and not plan.contact_sales and end is None:
        raise SubscriptionError("Paket berbayar perlu tanggal “berlaku sampai”.")
    if sub is None:
        sub = Subscription(store=store, plan=plan)
    sub.plan, sub.current_period_end = plan, end
    sub.auto_renew, sub.renew_months, sub.extra_outlets = bool(auto_renew), renew_months, extra_outlets
    sub.save()
    if end is not None:
        follow_paid_period(store, end)   # the lock date of a company on a trial or a paid period follows
    store.refresh_from_db()
    platform_log.log(actor, "subscription.created" if not before["berlaku_sampai"] and before["plan"].startswith(
        "(") else "subscription.updated", "Subscription", store.name,
        platform_log.diff(before, snapshot(Store_fresh(store))), store=store)
    return sub


def Store_fresh(store):
    from apps.shops.models import Store
    return Store.objects.get(pk=store.pk)
