"""Ajak teman: a store's invite link is lakuin.store/start/?ref=<its slug>.

The new store gets one discounted month for its first paid plan, and the inviting store gets
one once the new store pays. Each reward is REFERRAL_DISCOUNT_PERCENT off one month of the
plan; a monthly bill uses at most one, a yearly bill at most twelve."""
import re
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.shops.models import AuditLog, Store

from .models import Referral, ReferralReward

SESSION_KEY = "referral_code"
CODE = re.compile(r"^[a-z0-9-]{3,40}$")
# A discount never takes more than this share of a bill, so a bill is never free.
MAX_SHARE = Decimal("0.9")


def discount_percent():
    return max(1, min(90, int(getattr(settings, "REFERRAL_DISCOUNT_PERCENT", 50))))


def clean_code(value):
    value = (value or "").strip().lower()
    return value if CODE.fullmatch(value) else ""


def referrer_for(code):
    code = clean_code(code)
    return Store.objects.filter(slug=code, is_active=True).first() if code else None


def invite_url(store):
    from apps.shops.tenancy import platform_url
    return platform_url(f"/start/?ref={store.slug}")


def record_signup(referrer, store, user):
    """Link a new store to the store that invited it, with its welcome discount. A store
    inviting itself (same owner or same WhatsApp number) is recorded but earns nothing."""
    if referrer is None or referrer.id == store.id:
        return None
    from apps.shops.auth import login_phone

    same_owner = referrer.owner_id == user.id
    same_phone = bool(store.support_phone) and (
        login_phone(referrer.support_phone) == login_phone(store.support_phone))
    with transaction.atomic():
        referral = Referral.objects.create(referrer=referrer, referred=store,
            status="rejected" if same_owner or same_phone else "signed_up",
            note="Pemilik atau nomor WhatsApp sama" if same_owner or same_phone else "")
        if referral.status == "signed_up":
            ReferralReward.objects.create(store=store, referral=referral, kind="welcome",
                percent=discount_percent())
        AuditLog.objects.create(store=store, actor=user, action="billing.referral_signup",
            object_type="Referral", object_id=str(referral.id),
            details={"referrer": referrer.slug, "status": referral.status})
    return referral


def _available(store):
    """Unspent rewards not held by an invoice that may still be paid."""
    return ReferralReward.objects.filter(store=store, used_at__isnull=True).filter(
        Q(invoice__isnull=True) | ~Q(invoice__status__in=["pending", "paid"]))


def apply_rewards(invoice):
    """Take the store's free rewards off a new plan bill (not an outlet top-up). Call before
    any payment page exists for the bill: the page carries the amount."""
    if invoice.kind == "outlets" or invoice.status != "pending":
        return Decimal(0)
    monthly = invoice.plan.price_monthly or Decimal(0)
    if monthly <= 0:
        return Decimal(0)
    with transaction.atomic():
        # A checkout the seller abandoned gives its rewards back to the new bill.
        ReferralReward.objects.filter(store=invoice.store, used_at__isnull=True,
            invoice__status="pending", invoice__kind="checkout").exclude(invoice=invoice).update(
            invoice=None)
        rewards = list(_available(invoice.store).select_for_update()[:invoice.months])
        if not rewards:
            return Decimal(0)
        wanted = sum((monthly * reward.percent / 100 for reward in rewards), Decimal(0))
        discount = min(wanted, invoice.amount * MAX_SHARE).quantize(Decimal(1))
        if discount <= 0:
            return Decimal(0)
        invoice.amount -= discount
        invoice.discount += discount
        invoice.save(update_fields=["amount", "discount"])
        ReferralReward.objects.filter(id__in=[reward.id for reward in rewards]).update(
            invoice=invoice)
    return discount


def invoice_paid(invoice):
    """Spend the invoice's rewards and, on a store's first paid plan, reward its inviter."""
    now = timezone.now()
    invoice.referral_rewards.filter(used_at__isnull=True).update(used_at=now)
    if invoice.kind == "outlets":
        return
    referral = Referral.objects.select_for_update().filter(referred=invoice.store,
        status="signed_up").first()
    if referral is None:
        return
    referral.status, referral.qualified_at = "qualified", now
    referral.save(update_fields=["status", "qualified_at"])
    ReferralReward.objects.get_or_create(referral=referral, kind="referrer",
        defaults={"store": referral.referrer, "percent": discount_percent()})
    AuditLog.objects.create(store=referral.referrer, action="billing.referral_rewarded",
        object_type="Referral", object_id=str(referral.id),
        details={"referred": invoice.store.slug, "percent": discount_percent()})


def referral_summary(store):
    referrals = store.referrals_made.all()
    available = _available(store).count()
    return {"invite_url": invite_url(store), "percent": discount_percent(),
        "invited": referrals.exclude(status="rejected").count(),
        "qualified": referrals.filter(status="qualified").count(),
        "available": available,
        "held": store.referral_rewards.filter(used_at__isnull=True,
            invoice__status="pending").count(),
        "used": store.referral_rewards.filter(used_at__isnull=False).count(),
        "recent": referrals.select_related("referred")[:10]}
