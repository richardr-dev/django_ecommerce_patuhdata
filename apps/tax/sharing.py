"""Sending an invoice: a secret link the buyer opens, a WhatsApp message, an email.

The link (``/i/<token>/``) shows the invoice read-only with how to pay, and records the first time
it was opened. Sending by WhatsApp opens the seller's WhatsApp with the message ready (no API
needed); email goes through the configured Django backend. A reminder is the same message with the
amount still due and how late it is."""
import re
import secrets

from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone

from .models import Invoice


class ShareError(ValueError):
    pass


def ensure_token(invoice):
    if not invoice.public_token:
        invoice.public_token = secrets.token_urlsafe(24)
        invoice.save(update_fields=["public_token"])
    return invoice.public_token


def public_url(request, invoice):
    return request.build_absolute_uri(reverse("invoice-public", args=[ensure_token(invoice)]))


def _rupiah(value):
    return "Rp" + f"{value:,.0f}".replace(",", ".")


def message_text(invoice, url, amount_due):
    """The text sent with the link: a plain invoice, or a reminder when it is late."""
    today = timezone.localdate()
    late = (today - invoice.due_date).days if invoice.due_date and invoice.due_date < today else 0
    name = invoice.buyer_name
    if amount_due <= 0:
        return (f"Halo {name}, terima kasih! Pembayaran invoice {invoice.number} dari "
            f"{invoice.seller_name} sudah kami terima. Bukti invoice: {url}")
    if late:
        return (f"Halo {name}, kami mengingatkan invoice {invoice.number} dari {invoice.seller_name} "
            f"sebesar {_rupiah(amount_due)} sudah lewat jatuh tempo {late} hari. Rincian dan cara "
            f"pembayaran: {url} . Terima kasih.")
    due = f", jatuh tempo {invoice.due_date:%d %b %Y}" if invoice.due_date else ""
    return (f"Halo {name}, berikut invoice {invoice.number} dari {invoice.seller_name} sebesar "
        f"{_rupiah(amount_due)}{due}. Rincian dan cara pembayaran: {url} . Terima kasih.")


def whatsapp_link(phone, text):
    from urllib.parse import quote
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return f"https://wa.me/{digits}?text={quote(text)}" if len(digits) >= 9 else \
        f"https://wa.me/?text={quote(text)}"


def mark_sent(invoice):
    Invoice.objects.filter(pk=invoice.pk).update(sent_at=timezone.now())


def send_email(invoice, url, to, amount_due, *, note=""):
    address = (to or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise ShareError("Masukkan alamat email yang valid.")
    body = message_text(invoice, url, amount_due)
    if note:
        body = f"{note.strip()}\n\n{body}"
    EmailMessage(subject=f"Invoice {invoice.number} dari {invoice.seller_name}", body=body,
        to=[address]).send(fail_silently=False)
    mark_sent(invoice)


def record_view(invoice):
    if invoice.viewed_at is None:
        Invoice.objects.filter(pk=invoice.pk, viewed_at__isnull=True).update(viewed_at=timezone.now())
