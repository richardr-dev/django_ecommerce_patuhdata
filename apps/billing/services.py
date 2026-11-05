"""Seller subscription payments. They go to the platform's own payment account, unlike
storefront orders, so they never touch Order, inventory or the store's reconciliation."""
import math
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider
from apps.orders.models import PaymentEvent
from apps.shops.models import AuditLog
from apps.shops.tenancy import platform_url

from .models import Subscription, SubscriptionCheckout, SubscriptionInvoice
from .plans import PERIOD_DAYS, plan_amount
from .referrals import apply_rewards, invoice_paid

# DOKU invoice numbers for subscriptions; storefront orders use "NK".
SUBSCRIPTION_PREFIX = "SB"
SUBSCRIPTION_PROVIDERS = {"demo", "doku"}


class BillingError(Exception):
    pass


def start_checkout(store, plan, months, user):
    if not plan.is_paid or not plan.is_public or plan.contact_sales or months not in PERIOD_DAYS:
        raise BillingError("Paket atau periode tidak tersedia.")
    if plan.price_for(months) <= 0:
        raise BillingError("Harga paket untuk periode ini belum diatur.")
    current = getattr(store, "subscription", None)
    # Renewing the same plan pays for the extra outlets too; another plan starts without them.
    extra = current.extra_outlets if current and current.plan_id == plan.id else 0
    amount = plan_amount(plan, months, extra)
    provider = settings.PAYMENT_PROVIDER
    if provider not in SUBSCRIPTION_PROVIDERS:
        raise BillingError("Pembayaran langganan saat ini hanya tersedia melalui DOKU.")
    if provider == "demo" and not settings.ALLOW_DEMO_PAYMENTS:
        raise BillingError("Pembayaran langganan belum dikonfigurasi. Hubungi pengelola platform.")
    invoice = SubscriptionInvoice.objects.create(store=store, plan=plan, months=months,
        amount=amount, provider=provider, extra_outlets=extra)
    apply_rewards(invoice)  # before the payment page, which carries the amount
    amount = invoice.amount
    if provider == "demo":
        invoice.payment_reference = f"demo-{invoice.id.hex}"
        invoice.checkout_url = reverse("billing-demo-pay", args=[invoice.id])
    else:
        invoice.payment_reference = f"{SUBSCRIPTION_PREFIX}{invoice.id.hex[:26]}"
        customer = {"name": store.name[:100]}
        if user.email:
            customer["email"] = user.email
        try:
            invoice.checkout_url = DokuPaymentProvider().create_checkout(
                invoice_number=invoice.payment_reference, amount=int(amount), customer=customer,
                request_id=invoice.id.hex, callback_url=platform_url(reverse("merchant-billing")))
        except ProviderError as error:
            invoice.status = "failed"
            invoice.save(update_fields=["payment_reference", "status"])
            raise BillingError("Halaman pembayaran belum dapat dibuat. Coba lagi beberapa saat.") from error
    invoice.save(update_fields=["payment_reference", "checkout_url"])
    AuditLog.objects.create(store=store, actor=user, action="billing.checkout_started",
        object_type="SubscriptionInvoice", object_id=str(invoice.id),
        details={"plan": plan.code, "months": months, "amount": str(amount)})
    return invoice


@transaction.atomic
def mark_invoice_paid(invoice_id, *, amount, currency="IDR"):
    """Activate or extend the plan once. Returns a mismatch reason, or "" when applied."""
    invoice = SubscriptionInvoice.objects.select_for_update().select_related("plan").get(id=invoice_id)
    if invoice.status == "paid":
        return ""
    try:
        paid = Decimal(str(amount))
    except InvalidOperation:
        paid = Decimal(-1)
    if paid != invoice.amount or currency != "IDR":
        return "Amount or currency mismatch"
    now = timezone.now()
    subscription, _ = Subscription.objects.select_for_update().get_or_create(
        store=invoice.store, defaults={"plan": invoice.plan})
    if invoice.kind == "outlets":
        # Outlets added mid-period: the period stays, the outlet count grows, and an open
        # renewal bill is brought up to date if it can still change.
        subscription.extra_outlets += invoice.extra_outlets
        subscription.save(update_fields=["extra_outlets", "updated_at"])
        invoice.status, invoice.paid_at = "paid", now
        invoice.save(update_fields=["status", "paid_at"])
        invoice_paid(invoice)
        _refresh_open_renewal(subscription)
        AuditLog.objects.create(store=invoice.store, action="billing.outlets_added",
            object_type="SubscriptionInvoice", object_id=str(invoice.id),
            details={"added": invoice.extra_outlets, "extra_outlets": subscription.extra_outlets})
        return ""
    renewing = (subscription.plan_id == invoice.plan_id and subscription.current_period_end
        and subscription.current_period_end > now)
    start = subscription.current_period_end if renewing else now
    end = start + timedelta(days=PERIOD_DAYS[invoice.months])
    subscription.plan, subscription.current_period_end = invoice.plan, end
    # The next renewal bills the period length the seller just chose.
    subscription.renew_months = invoice.months
    if not invoice.plan.extra_outlet_price:
        subscription.extra_outlets = 0  # a plan without extra outlets
    subscription.save(update_fields=["plan", "current_period_end", "renew_months",
        "extra_outlets", "updated_at"])
    invoice.status, invoice.paid_at = "paid", now
    invoice.period_start, invoice.period_end = start, end
    invoice.save(update_fields=["status", "paid_at", "period_start", "period_end"])
    invoice_paid(invoice)
    # Any other open renewal bill is settled by this payment.
    SubscriptionInvoice.objects.filter(store=invoice.store, kind="renewal",
        status="pending").exclude(id=invoice.id).update(status="cancelled")
    AuditLog.objects.create(store=invoice.store, action="billing.subscription_paid",
        object_type="SubscriptionInvoice", object_id=str(invoice.id),
        details={"plan": invoice.plan.code, "period_end": end.isoformat()})
    return ""


def handle_doku_notification(invoice_number, request_id, payload):
    """Signed DOKU SUCCESS notification for a subscription. Returns (body, status)."""
    checkout = SubscriptionCheckout.objects.filter(reference=invoice_number).select_related(
        "invoice").first()
    invoice = checkout.invoice if checkout else SubscriptionInvoice.objects.filter(
        payment_reference=invoice_number, provider="doku").first()
    if not invoice:
        return {"error": "Invoice not found"}, 404
    _, created = PaymentEvent.objects.get_or_create(provider="doku", external_id=request_id,
        defaults={"store": invoice.store, "event_type": "SUCCESS", "payload": payload})
    if not created:
        return {"received": True, "duplicate": True}, 200
    order = payload.get("order") or {}
    mismatch = mark_invoice_paid(invoice.id, amount=order.get("amount", -1),
        currency=order.get("currency", "IDR"))
    if mismatch:
        return {"error": mismatch}, 400
    return {"received": True}, 200


def check_pending_invoice(invoice):
    """Ask DOKU directly, for when a notification was lost. Returns the new status."""
    if invoice.status != "pending" or invoice.provider != "doku":
        return invoice.status
    try:
        status = DokuPaymentProvider().fetch_status(invoice)
    except ProviderError:
        return invoice.status
    if status.state == "paid":
        if not mark_invoice_paid(invoice.id, amount=status.amount, currency=status.currency):
            return "paid"
    elif status.state == "unpaid" and invoice.kind == "checkout" and invoice.created_at < timezone.now() - timedelta(
            minutes=settings.DOKU_PAYMENT_DUE_MINUTES + 5):
        SubscriptionInvoice.objects.filter(id=invoice.id, status="pending").update(status="cancelled")
        return "cancelled"
    return invoice.status


def _refresh_open_renewal(subscription):
    """Re-price an open renewal bill nobody has started paying, after the outlet count changed."""
    renewal = subscription.store.subscription_invoices.filter(kind="renewal",
        status="pending").first()
    if renewal and not renewal.checkouts.exists():
        renewal.extra_outlets = subscription.extra_outlets
        # Keep the referral discount the bill already carries.
        renewal.amount = max(Decimal(1), plan_amount(renewal.plan, renewal.months,
            subscription.extra_outlets) - renewal.discount)
        renewal.save(update_fields=["extra_outlets", "amount"])


def outlet_purchase_quote(subscription, count):
    """Price of adding `count` outlets for the rest of the current period, rounded up to the
    next thousand rupiah."""
    plan = subscription.plan
    if not plan.extra_outlet_price or count < 1:
        raise BillingError("Paket ini tidak bisa menambah outlet. Upgrade ke Pro.")
    end = subscription.current_period_end
    if not end or end <= timezone.now():
        raise BillingError("Perpanjang paket terlebih dahulu, lalu tambah outlet.")
    remaining_days = max(1, round((end - timezone.now()).total_seconds() / 86400))
    raw = plan.extra_outlet_price * count * remaining_days / Decimal(30)
    return Decimal(math.ceil(raw / 1000) * 1000), remaining_days


def start_outlet_purchase(store, count, user):
    subscription = Subscription.objects.select_related("plan").filter(store=store).first()
    if subscription is None:
        raise BillingError("Toko ini dikelola langsung oleh platform tanpa paket langganan.")
    amount, days = outlet_purchase_quote(subscription, count)
    invoice = SubscriptionInvoice.objects.create(store=store, plan=subscription.plan, months=1,
        amount=amount, provider=settings.PAYMENT_PROVIDER, kind="outlets", extra_outlets=count)
    AuditLog.objects.create(store=store, actor=user, action="billing.outlets_ordered",
        object_type="SubscriptionInvoice", object_id=str(invoice.id),
        details={"count": count, "days": days, "amount": str(amount)})
    return invoice


def reduce_outlets(store, extra_outlets, active_outlets):
    """Lower the paid extra outlets from now on; the next renewal bills the new count."""
    subscription = Subscription.objects.select_related("plan").filter(store=store).first()
    if subscription is None:
        raise BillingError("Toko ini dikelola langsung oleh platform tanpa paket langganan.")
    included = subscription.plan.included_outlets or 0
    if extra_outlets >= subscription.extra_outlets:
        raise BillingError("Untuk menambah outlet, gunakan tombol Tambah outlet.")
    if active_outlets > included + extra_outlets:
        raise BillingError(f"Nonaktifkan dulu outlet yang tidak dipakai: ada {active_outlets} "
            f"outlet aktif, sedangkan paket akan memuat {included + extra_outlets}.")
    subscription.extra_outlets = extra_outlets
    subscription.save(update_fields=["extra_outlets", "updated_at"])
    _refresh_open_renewal(subscription)
    return subscription
