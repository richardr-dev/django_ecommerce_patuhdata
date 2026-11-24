"""Free trial of the back office. A company with ``Store.trial_ends_at`` set may use everything until that
moment; afterwards every back-office and cash-register page answers with a "contact sales" notice and no
write goes through. The partner (sales) extends the trial or switches the company to full access from the
install page. A company with no trial date has full access, so nothing installed before this existed locks.
Platform superusers are never locked."""
from datetime import timedelta

from django.conf import settings
from django.shortcuts import render
from django.utils import timezone

from .models import AuditLog, Store

LOCKED_PREFIXES = ("/books/", "/kasir/")
# Still reachable after the trial ends: signing out and setting a first password from an invite.
# The billing pages stay open so a locked company can pay and unlock itself.
EXEMPT_PREFIXES = ("/books/logout/", "/kasir/keluar/", "/books/invite/", "/books/billing/")


def trial_state(store):
    """None for full access, else ``{"ends_at", "days_left", "expired"}``."""
    end = store.trial_ends_at
    if end is None:
        return None
    left = (end - timezone.now()).total_seconds()
    paid = False
    if left <= 0:
        # A paying company a few days late with its renewal keeps working (same grace as the plan limits).
        paid = store.subscription_invoices.filter(status="paid").exists()
        if paid and timezone.now() < end + timedelta(days=settings.RENEWAL_GRACE_DAYS):
            left = 1
    elif store.subscription_invoices.filter(status="paid").exists():
        paid = True
    return {"ends_at": end, "expired": left <= 0, "days_left": max(0, -(-int(left) // 86400)), "paid": paid}


def is_expired(store):
    state = trial_state(store)
    return bool(state and state["expired"])


def start_trial(store, days):
    store.trial_ends_at = timezone.now() + timedelta(days=int(days))
    store.save(update_fields=["trial_ends_at"])
    return store


def extend_trial(store, days, actor=None):
    """Add days to what is left (or to today when it has already ended)."""
    base = max(store.trial_ends_at or timezone.now(), timezone.now())
    store.trial_ends_at = base + timedelta(days=int(days))
    store.save(update_fields=["trial_ends_at"])
    _log(store, actor, "trial.extended", days=days)
    return store


def follow_paid_period(store, period_end):
    """A payment moves the lock date to the end of the period just paid for. A company the partner gave full
    access (no date) stays that way."""
    if store.trial_ends_at is not None:
        store.trial_ends_at = period_end
        store.save(update_fields=["trial_ends_at"])


def set_trial_end(store, end, actor=None):
    """Set the exact end of the trial (a datetime), or None for full access."""
    store.trial_ends_at = end
    store.save(update_fields=["trial_ends_at"])
    _log(store, actor, "trial.set", ends=end or "full access")
    return store


def activate_full_access(store, actor=None):
    store.trial_ends_at = None
    store.save(update_fields=["trial_ends_at"])
    _log(store, actor, "trial.activated")
    return store


def end_trial_now(store, actor=None):
    store.trial_ends_at = timezone.now()
    store.save(update_fields=["trial_ends_at"])
    _log(store, actor, "trial.ended")
    return store


def _log(store, actor, action, **details):
    AuditLog.objects.create(store=store, actor=actor, action=action, object_type="Store",
        object_id=str(store.pk), details={k: str(v) for k, v in details.items()})
    from . import platform_log
    platform_log.log(actor, action, "Store", store.name, {k: ["", str(v)] for k, v in details.items()}
        | {"masa_uji_coba_berakhir": ["", store.trial_ends_at.date().isoformat() if store.trial_ends_at else "akses penuh"]},
        store=store)


def sales_contact():
    """What the notice offers: a WhatsApp link and an email, whichever the platform has set."""
    from .models import PlatformSettings
    stored = PlatformSettings.objects.filter(pk=1).first()
    shown = (stored.sales_whatsapp if stored else "") or settings.SALES_WHATSAPP
    email = (stored.sales_email if stored else "") or settings.SALES_EMAIL
    phone = "".join(ch for ch in shown if ch.isdigit())
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    return {"whatsapp": shown, "whatsapp_url": f"https://wa.me/{phone}" if phone else "", "email": email}


def _store_of(request):
    """The company this request is for: the same choice the back office makes (?store=, the session, else the
    first company the person owns or works for)."""
    user = request.user
    stores = (Store.objects.filter(owner=user) | Store.objects.filter(staff_members__user=user)).distinct()
    requested = str(request.GET.get("store") or "").strip()
    if requested.isdigit() and stores.filter(id=requested).exists():
        return stores.get(id=requested)
    return (stores.filter(id=request.session.get("merchant_store_id")).first()
        or stores.order_by("name").first())


class TrialLockMiddleware:
    """After the trial: every back-office and cash-register request gets the notice (status 402, so a soft
    navigation falls back to a full page and a form post or htmx call changes nothing)."""
    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        path = request.path
        user = getattr(request, "user", None)
        if (path.startswith(LOCKED_PREFIXES) and not path.startswith(EXEMPT_PREFIXES)
                and user is not None and user.is_authenticated and not user.is_superuser):
            store = _store_of(request)
            if store is not None and is_expired(store):
                from apps.shops.permissions import roles
                role = "admin" if store.owner_id == user.id else getattr(
                    store.staff_members.filter(user=user).first(), "role", "")
                return render(request, "platform/trial_ended.html", {"store": store,
                    "contact": sales_contact(), "ended_at": store.trial_ends_at,
                    "was_paid": trial_state(store)["paid"], "can_pay": role in roles("billing.manage")},
                    status=402)
        return self.get_response(request)
