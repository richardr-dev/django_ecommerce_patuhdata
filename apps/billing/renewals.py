"""Monthly renewal billing. Before a paid period (or a trial) ends, Lakuin bills the next period
and reminds the seller to pay it; paying extends the plan from where it ended.

Payment stays one click (QRIS, virtual account, e-wallet or card on DOKU's page) rather than an
automatic card charge, because most Indonesian sellers pay without a card and charging a saved
card needs DOKU's recurring product approved for the platform."""
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider
from apps.shops.models import AuditLog
from apps.shops.tenancy import platform_url

from .models import Subscription, SubscriptionCheckout, SubscriptionInvoice

logger = logging.getLogger(__name__)

# Days before the period end (negative: after it) at which a reminder goes out.
REMINDER_DAYS = [7, 3, 1, 0, -3]
STALE_AFTER_DAYS = 30


class RenewalError(Exception):
    pass


def pending_renewal(store):
    return store.subscription_invoices.filter(kind="renewal", status="pending").order_by(
        "-due_at").select_related("plan").first()


def _renewal_for(subscription):
    """The pending renewal bill for this period, created on first call."""
    plan, months = subscription.plan, subscription.renew_months
    amount = plan.price_for(months) or plan.price_monthly * months
    if amount <= 0:
        return None
    try:
        with transaction.atomic():
            invoice, created = SubscriptionInvoice.objects.get_or_create(
                store=subscription.store, kind="renewal", due_at=subscription.current_period_end,
                defaults={"plan": plan, "months": months, "amount": amount,
                    "provider": settings.PAYMENT_PROVIDER})
    except IntegrityError:  # created by a concurrent worker
        invoice, created = SubscriptionInvoice.objects.get(store=subscription.store,
            kind="renewal", due_at=subscription.current_period_end), False
    if created:
        AuditLog.objects.create(store=subscription.store, action="billing.renewal_billed",
            object_type="SubscriptionInvoice", object_id=str(invoice.id),
            details={"plan": plan.code, "months": months, "amount": str(amount)})
    return invoice if invoice.status == "pending" else None


def _recipients(store):
    return sorted({address for address in (store.owner.email, store.support_email) if address})


def send_reminder(invoice, days_left):
    store = invoice.store
    recipients = _recipients(store)
    if not recipients:
        return False
    when = (f"berakhir dalam {days_left} hari" if days_left > 0 else
        "berakhir hari ini" if days_left == 0 else f"sudah berakhir {-days_left} hari lalu")
    trial = not store.subscription_invoices.filter(status="paid").exists()
    subject = (f"{store.name}: masa coba {settings.PLATFORM_NAME} {when}" if trial else
        f"{store.name}: paket {invoice.plan.name} {when}")
    link = platform_url(reverse("merchant-billing") + f"?store={store.id}")
    body = (f"Halo,\n\n{'Masa coba gratis' if trial else 'Paket ' + invoice.plan.name} toko "
        f"{store.name} {when} ({timezone.localtime(invoice.due_at):%d %b %Y}).\n\n"
        f"Tagihan {invoice.get_months_display()} paket {invoice.plan.name}: "
        f"Rp{invoice.amount:,.0f}".replace(",", ".") + "\n"
        f"Bayar dengan QRIS, virtual account, e-wallet, atau kartu:\n{link}\n\n"
        "Masa aktif baru dihitung dari akhir periode sekarang, jadi tidak ada hari yang hilang. "
        "Bila tidak diperpanjang, toko tetap tampil tetapi produk baru belum bisa ditambahkan.\n\n"
        f"Matikan perpanjangan otomatis kapan saja di menu Langganan.\n\n{settings.PLATFORM_NAME}"
        f" · {settings.PLATFORM_COMPANY_NAME}")
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)
    except Exception:  # an unreachable mail server must not stop renewals for other stores
        logger.exception("renewal reminder email failed for store %s", store.pk)
        return False
    return True


def run_renewals(now=None):
    """Bill upcoming renewals, send due reminders, retire stale bills. Returns reminders sent."""
    now = now or timezone.now()
    sent = 0
    window_start = now - timedelta(days=1 - min(REMINDER_DAYS))
    subscriptions = Subscription.objects.filter(auto_renew=True, plan__price_monthly__gt=0,
        current_period_end__isnull=False, current_period_end__gt=window_start,
        current_period_end__lte=now + timedelta(days=settings.RENEWAL_NOTICE_DAYS),
        store__is_active=True).select_related("store__owner", "plan")
    for subscription in subscriptions:
        invoice = _renewal_for(subscription)
        if invoice is None:
            continue
        remaining = subscription.current_period_end - now
        due_count = sum(1 for day in REMINDER_DAYS if remaining <= timedelta(days=day))
        hours = remaining.total_seconds() / 3600
        days_left = math.ceil(hours / 24) if hours > 0 else -math.ceil(-hours / 24)
        if invoice.reminders_sent < due_count and send_reminder(invoice, days_left):
            SubscriptionInvoice.objects.filter(id=invoice.id).update(reminders_sent=due_count)
            sent += 1
    SubscriptionInvoice.objects.filter(kind="renewal", status="pending",
        due_at__lt=now - timedelta(days=STALE_AFTER_DAYS)).update(status="cancelled")
    return sent


def open_payment_page(invoice, user):
    """A fresh payment page for a pending invoice. Returns its URL."""
    from .services import SUBSCRIPTION_PREFIX, SUBSCRIPTION_PROVIDERS

    if invoice.status != "pending":
        raise RenewalError("Tagihan ini sudah tidak menunggu pembayaran.")
    provider = settings.PAYMENT_PROVIDER
    if provider not in SUBSCRIPTION_PROVIDERS or (provider == "demo" and
            not settings.ALLOW_DEMO_PAYMENTS):
        raise RenewalError("Pembayaran langganan belum dikonfigurasi. Hubungi pengelola platform.")
    if invoice.payment_reference:
        # Keep the page opened at checkout matchable before a new one replaces it.
        SubscriptionCheckout.objects.get_or_create(reference=invoice.payment_reference,
            defaults={"invoice": invoice, "checkout_url": invoice.checkout_url})
    attempt = invoice.checkouts.count() + 1
    if provider == "demo":
        reference, url = f"demo-{invoice.id.hex}-{attempt}", reverse("billing-demo-pay",
            args=[invoice.id])
    else:
        reference = f"{SUBSCRIPTION_PREFIX}{invoice.id.hex[:24]}R{attempt}"
        customer = {"name": invoice.store.name[:100]}
        if user.email:
            customer["email"] = user.email
        try:
            url = DokuPaymentProvider().create_checkout(invoice_number=reference,
                amount=int(invoice.amount), customer=customer,
                request_id=f"{invoice.id.hex}-{attempt}",
                callback_url=platform_url(reverse("merchant-billing")))
        except ProviderError as error:
            raise RenewalError("Halaman pembayaran belum dapat dibuat. Coba lagi beberapa saat."
                ) from error
    SubscriptionCheckout.objects.create(invoice=invoice, reference=reference, checkout_url=url)
    # The newest page is the one a status check asks DOKU about.
    SubscriptionInvoice.objects.filter(id=invoice.id).update(provider=provider,
        payment_reference=reference, checkout_url=url)
    AuditLog.objects.create(store=invoice.store, actor=user, action="billing.payment_page_opened",
        object_type="SubscriptionInvoice", object_id=str(invoice.id), details={"attempt": attempt})
    return url
