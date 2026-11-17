"""Automatic payment reminders: a store's policy (days before due, days after due, by email and
WhatsApp) and the daily run that sends them. One reminder per invoice, step and channel, ever
(``ReminderLog``), so the worker may run as often as it likes.

Email goes to the customer's address; WhatsApp through the store's own connected account
(``integrations/whatsapp.py``), never from a number of the platform. The text is the same the seller
sends by hand (``sharing.message_text``), with a "falls due in N days" variant before the date."""
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone

from apps.shops.tenancy import platform_url

from . import sharing
from .models import Invoice, ReminderLog, ReminderPolicy

DEFAULT_AFTER = "1,7,14"


def policy_for(store):
    """The store's policy, or the default (off) when none was saved yet."""
    return ReminderPolicy.objects.filter(store=store).first() or ReminderPolicy(store=store)


def offsets(policy):
    days = set()
    for part in (policy.days_after or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 < int(part) <= 365:
            days.add(int(part))
    return sorted(days)


def open_invoices(store):
    return Invoice.objects.filter(store=store, due_date__isnull=False).exclude(
        status__in=[Invoice.STATUS_VOID, Invoice.STATUS_DRAFT, Invoice.STATUS_PAID]).select_related(
        "order__customer")


def due_today(store, today=None):
    """``[(invoice, kind, offset)]`` that the policy says to send today."""
    today = today or timezone.localdate()
    policy = policy_for(store)
    if not policy.enabled:
        return []
    after = offsets(policy)
    out = []
    for invoice in open_invoices(store):
        if invoice.amount_due <= 0:
            continue
        ahead = (invoice.due_date - today).days
        if policy.days_before and ahead == policy.days_before:
            out.append((invoice, ReminderLog.BEFORE, policy.days_before))
        elif ahead < 0 and -ahead in after:
            out.append((invoice, ReminderLog.AFTER, -ahead))
    return out


def _text(invoice, url, kind, offset):
    if kind == ReminderLog.BEFORE:
        return sharing.upcoming_text(invoice, url, invoice.amount_due, offset)
    return sharing.message_text(invoice, url, invoice.amount_due)


def _send_one(policy, invoice, kind, offset, channel, to, text, account=None):
    log, created = ReminderLog.objects.get_or_create(invoice=invoice, kind=kind, offset=offset,
        channel=channel, defaults={"to": to, "status": ReminderLog.PENDING})
    if not created:
        return False
    try:
        if channel == ReminderLog.EMAIL:
            EmailMessage(subject=f"Pengingat invoice {invoice.number} dari {invoice.seller_name}",
                body=text, to=[to]).send(fail_silently=False)
        else:
            from apps.integrations.whatsapp import send
            message = send(account, kind="invoice_reminder", to=to, text=text, order=invoice.order)
            if message.status != "sent":
                raise OSError(message.error or "WhatsApp gagal")
    except Exception as error:  # noqa: BLE001 - a failed reminder is logged, never crashes the worker
        log.status, log.error = ReminderLog.FAILED, str(error)[:300]
        log.save(update_fields=["status", "error"])
        return False
    log.status = ReminderLog.SENT
    log.save(update_fields=["status"])
    sharing.mark_sent(invoice)
    return True


def run(store, today=None):
    """Send today's reminders for one store. Returns how many went out."""
    from apps.integrations.models import StoreWhatsAppSettings
    from apps.integrations.whatsapp import msisdn

    policy = policy_for(store)
    sent = 0
    account = StoreWhatsAppSettings.objects.filter(store=store).first() if policy.use_whatsapp else None
    if account is not None and not account.ready:
        account = None
    for invoice, kind, offset in due_today(store, today):
        url = platform_url(reverse("invoice-public", args=[sharing.ensure_token(invoice)]))
        text = _text(invoice, url, kind, offset)
        customer = invoice.order.customer if invoice.order.customer_id else None
        email = (customer.email if customer else "").strip()
        if policy.use_email and email:
            sent += _send_one(policy, invoice, kind, offset, ReminderLog.EMAIL, email, text)
        phone = msisdn(invoice.order.phone)
        if account is not None and phone:
            sent += _send_one(policy, invoice, kind, offset, ReminderLog.WHATSAPP, phone, text, account)
    return sent


def run_all(today=None):
    from apps.shops.models import Store
    return sum(run(store, today) for store in Store.objects.filter(reminder_policy__enabled=True))
