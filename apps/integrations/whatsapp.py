"""Notifikasi WhatsApp: order updates sent from the seller's own WhatsApp number, through the
seller's Fonnte device or Meta WhatsApp Cloud API account (Lakuin never sends from its own
number on a store's behalf). The outbox worker calls ``deliver`` for each order event; every
attempt is logged in WhatsAppMessage so a retried event never messages a buyer twice."""
import re

from django.urls import reverse
from django.utils import timezone

from .http import ProviderError, request_form_json, request_json
from .models import StoreWhatsAppSettings, WhatsAppMessage

FONNTE_URL = "https://api.fonnte.com/send"
GRAPH_URL = "https://graph.facebook.com/v21.0"

KINDS = {
    "order_created": "Pesanan dibuat",
    "payment_received": "Pembayaran diterima",
    "shipped": "Pesanan dikirim",
    "delivered": "Pesanan sampai",
    "cancelled": "Pesanan dibatalkan",
    "seller_new_order": "Notifikasi penjual",
    "test": "Tes",
}


class WhatsAppRejected(Exception):
    """The provider refused the message for good (e.g. not a WhatsApp number): no retry."""


def msisdn(phone):
    """An Indonesian phone number as WhatsApp wants it (628…), or "" when it is not one."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits if digits.startswith("628") and 10 <= len(digits) <= 15 else ""


class FonnteSender:
    """Fonnte (fonnte.com): a WhatsApp number linked by QR code; free-form text."""

    def __init__(self, account):
        self.token = account.token

    def send(self, to, text):
        data = request_form_json(FONNTE_URL, {"target": to, "message": text},
            headers={"Authorization": self.token}, attempts=1)
        if not data.get("status"):
            raise WhatsAppRejected(str(data.get("reason") or data.get("detail") or data)[:300])
        ids = data.get("id") or []
        return str(ids[0] if isinstance(ids, list) and ids else ids or "")


class CloudApiSender:
    """Meta WhatsApp Cloud API. Business-initiated messages must use an approved template, so
    the seller registers one utility template whose body is a single variable, {{1}}."""

    def __init__(self, account):
        self.account = account

    def send(self, to, text):
        # Template variables may not contain line breaks, tabs or long runs of spaces.
        flat = re.sub(r"\s*\n\s*", " · ", text.strip())
        flat = re.sub(r"[ \t]{2,}", " ", flat)[:1000]
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": self.account.template_name,
                "language": {"code": self.account.template_language or "id"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}]}}
        try:
            data = request_json(f"{GRAPH_URL}/{self.account.phone_number_id}/messages",
                method="POST", payload=payload, attempts=1,
                headers={"Authorization": f"Bearer {self.account.token}"})
        except ProviderError as error:
            if "HTTP 400" in str(error):  # bad number, template or parameter: retrying won't help
                raise WhatsAppRejected(str(error)[:300]) from error
            raise
        messages = data.get("messages") or [{}]
        return str(messages[0].get("id", ""))


SENDERS = {"fonnte": FonnteSender, "cloud_api": CloudApiSender}


def sender_for(account):
    return SENDERS[account.provider](account)


def _rupiah(amount):
    return f"Rp{amount:,.0f}".replace(",", ".")


def _order_link(order):
    from apps.orders.manual_orders import payment_page

    return payment_page(order)


def buyer_message(order, kind):
    store, link = order.store.name, _order_link(order)
    greeting = f"Halo {order.customer_name}, "
    if kind == "order_created":
        lines = [greeting + f"terima kasih sudah memesan di {store}."]
        lines += [f"• {item.quantity}× {item.name}" + (f" ({item.variant_name})"
            if item.variant_name else "") for item in order.items.all()]
        lines.append(f"Total: {_rupiah(order.total)}")
        if order.payment_expires_at:
            lines.append("Selesaikan pembayaran sebelum "
                f"{timezone.localtime(order.payment_expires_at):%d %b %Y %H:%M}:")
        else:
            lines.append("Selesaikan pembayaran di:")
        lines.append(link)
    elif kind == "payment_received":
        lines = [greeting + f"pembayaran {_rupiah(order.total)} untuk pesanan Anda di {store} "
            "sudah kami terima. Pesanan segera kami proses.", f"Detail pesanan: {link}"]
    elif kind == "shipped":
        lines = [greeting + f"pesanan Anda dari {store} sudah dikirim."]
        if order.shipping_service:
            lines.append(f"Kurir: {order.shipping_service}")
        if order.tracking_number:
            lines.append(f"No. resi: {order.tracking_number}")
        lines.append(f"Lacak pesanan: {link}")
    elif kind == "delivered":
        lines = [greeting + f"pesanan Anda dari {store} sudah sampai. Terima kasih sudah "
            "berbelanja!", f"Detail pesanan: {link}"]
    elif kind == "cancelled":
        lines = [greeting + f"pesanan Anda di {store} dibatalkan.",
            f"Detail pesanan: {link}"]
    else:
        raise ValueError(kind)
    lines.append(f"Kode pesanan: {str(order.id)[:8].upper()}")
    return "\n".join(lines)


def seller_message(order):
    from apps.shops.tenancy import platform_url

    items = ", ".join(f"{item.quantity}× {item.name}" for item in order.items.all())
    return "\n".join([f"Pesanan baru dibayar di {order.store.name}: {_rupiah(order.total)}",
        f"Pembeli: {order.customer_name} ({order.phone})", f"Produk: {items}",
        f"Proses di: {platform_url(reverse('merchant-orders'))}"])


STATUS_KINDS = {"shipped": "shipped", "delivered": "delivered", "cancelled": "cancelled"}


def messages_for(event, order, account):
    """(kind, phone, text) for each WhatsApp message this event should send."""
    if hasattr(order, "channel_order"):
        return []  # marketplace orders: the marketplace talks to its buyers
    topic, kind = event.topic, None
    if topic == "order.created" and order.status == "pending":
        kind = "order_created"
    elif topic == "payment.received":
        kind = "payment_received"
    elif topic == "order.status_changed":
        kind = STATUS_KINDS.get(event.payload.get("status"))
        if kind == "cancelled" and not order.payment_checkout_url and (
                event.payload.get("from") == "pending"):
            kind = None  # an abandoned checkout the buyer never saw a payment page for
    out = []
    if kind and getattr(account, f"notify_{kind}"):
        out.append((kind, msisdn(order.phone), buyer_message(order, kind)))
    if kind == "payment_received" and account.seller_alert_phone:
        out.append(("seller_new_order", msisdn(account.seller_alert_phone), seller_message(order)))
    return out


def send(account, *, kind, to, text, order=None, event=None):
    """Send one message and log it. Raises ProviderError when it is worth retrying."""
    message = WhatsAppMessage(store=account.store, order=order, outbox_event=event, kind=kind,
        to=to, body=text, provider=account.provider)
    try:
        message.provider_message_id = sender_for(account).send(to, text)[:120]
        message.status = "sent"
    except WhatsAppRejected as error:
        message.status, message.error = "failed", str(error)[:300]
    except ProviderError as error:
        message.status, message.error = "failed", str(error)[:300]
        message.save()
        raise
    message.save()
    return message


def deliver(event):
    """The outbox handler: send this event's WhatsApp messages that were not sent yet."""
    from apps.orders.models import Order

    account = StoreWhatsAppSettings.objects.filter(store=event.store).first()
    if not account or not account.ready:
        return {"skipped": "whatsapp not connected"}
    order = Order.objects.filter(id=event.payload.get("order_id"), store=event.store).first()
    if not order:
        return {"skipped": "event has no order"}
    result = {}
    for kind, to, text in messages_for(event, order, account):
        if not to:
            result[kind] = "no whatsapp number"
            continue
        if WhatsAppMessage.objects.filter(outbox_event=event, kind=kind, to=to,
                status="sent").exists():
            result[kind] = "already sent"
            continue
        result[kind] = send(account, kind=kind, to=to, text=text, order=order,
            event=event).status
    return result or {"skipped": "nothing to send"}
