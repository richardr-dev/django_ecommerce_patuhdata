"""Segmentasi pelanggan and email broadcast.

A segment is a set of filters over the store's customers, with order stats computed from
their real (paid, not cancelled) orders, matched by WhatsApp number. A broadcast emails one
segment; only customers who agreed to promotions get it, each with an unsubscribe link."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import (
    Count,
    DecimalField,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .models import Customer, EmailBroadcast, EmailBroadcastRecipient
from .tenancy import store_url

logger = logging.getLogger(__name__)
UNSOLD = ["pending", "payment_failed", "cancelled", "refunded"]
UNSUBSCRIBE_SALT = "lakuin.customer-unsubscribe"
BATCH = 50


def with_stats(queryset):
    from apps.orders.models import Order

    orders = Order.objects.filter(store=OuterRef("store"), phone=OuterRef("phone")).exclude(
        status__in=UNSOLD).values("phone")
    return queryset.annotate(
        order_count=Coalesce(Subquery(orders.annotate(n=Count("id")).values("n")), Value(0),
            output_field=IntegerField()),
        total_spent=Coalesce(Subquery(orders.annotate(t=Sum("total")).values("t")), Value(Decimal(0)),
            output_field=DecimalField(max_digits=14, decimal_places=2)),
        last_order_at=Subquery(orders.annotate(last=Max("created_at")).values("last")))


def _int(value):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse(params):
    """The segment filters from a GET/POST dict, cleaned."""
    segment = {}
    for key in ("tag", "q"):
        value = (params.get(key) or "").strip()[:60]
        if value:
            segment[key] = value
    for key in ("min_orders", "min_spend", "active_days", "inactive_days"):
        value = _int(params.get(key))
        if value:
            segment[key] = value
    for key in ("has_email", "marketing"):
        if params.get(key) in {"1", "on", True}:
            segment[key] = True
    return segment


def describe(segment):
    parts = []
    if segment.get("tag"):
        parts.append(f"tag {segment['tag']}")
    if segment.get("min_orders"):
        parts.append(f"≥ {segment['min_orders']} pesanan")
    if segment.get("min_spend"):
        parts.append(f"belanja ≥ Rp{segment['min_spend']:,}".replace(",", "."))
    if segment.get("active_days"):
        parts.append(f"belanja dalam {segment['active_days']} hari terakhir")
    if segment.get("inactive_days"):
        parts.append(f"tidak belanja ≥ {segment['inactive_days']} hari")
    if segment.get("marketing"):
        parts.append("setuju promo")
    if segment.get("has_email"):
        parts.append("punya email")
    if segment.get("q"):
        parts.append(f"cari \"{segment['q']}\"")
    return ", ".join(parts) or "Semua pelanggan"


def customers_in(store, segment):
    customers = with_stats(Customer.objects.filter(store=store))
    if segment.get("q"):
        query = segment["q"]
        customers = customers.filter(Q(name__icontains=query) | Q(phone__icontains=query)
            | Q(email__icontains=query))
    if segment.get("tag"):
        tag = segment["tag"].lower()
        customers = customers.filter(Q(tags__iexact=tag) | Q(tags__istartswith=f"{tag},")
            | Q(tags__iendswith=f", {tag}") | Q(tags__iendswith=f",{tag}")
            | Q(tags__icontains=f", {tag},") | Q(tags__icontains=f",{tag},"))
    if segment.get("min_orders"):
        customers = customers.filter(order_count__gte=segment["min_orders"])
    if segment.get("min_spend"):
        customers = customers.filter(total_spent__gte=segment["min_spend"])
    now = timezone.now()
    if segment.get("active_days"):
        customers = customers.filter(last_order_at__gte=now - timedelta(days=segment["active_days"]))
    if segment.get("inactive_days"):
        customers = customers.filter(Q(last_order_at__lt=now - timedelta(days=segment["inactive_days"]))
            | Q(last_order_at__isnull=True))
    if segment.get("marketing"):
        customers = customers.filter(accepts_marketing=True)
    if segment.get("has_email"):
        customers = customers.exclude(email="")
    return customers


def add_tag(customers, tag):
    tag = tag.strip().replace(",", " ")[:40]
    if not tag:
        return 0
    changed = 0
    for customer in customers.only("id", "tags"):
        if tag.lower() not in [existing.lower() for existing in customer.tag_list]:
            customer.tags = ", ".join([*customer.tag_list, tag])[:300]
            customer.save(update_fields=["tags"])
            changed += 1
    return changed


def broadcast_audience(store, segment):
    return customers_in(store, segment).filter(accepts_marketing=True).exclude(email="")


@transaction.atomic
def queue_broadcast(store, user, subject, body, segment):
    broadcast = EmailBroadcast.objects.create(store=store, subject=subject, body=body,
        segment=segment, segment_label=describe(segment), created_by=user)
    rows, seen = [], set()
    for customer in broadcast_audience(store, segment).only("id", "email"):
        email = customer.email.strip().lower()
        if email and email not in seen:
            seen.add(email)
            rows.append(EmailBroadcastRecipient(broadcast=broadcast, customer=customer, email=email))
    EmailBroadcastRecipient.objects.bulk_create(rows, batch_size=500)
    broadcast.recipients = len(rows)
    if not rows:
        broadcast.status, broadcast.finished_at = "sent", timezone.now()
    broadcast.save(update_fields=["recipients", "status", "finished_at"])
    return broadcast


def unsubscribe_url(store, customer):
    token = signing.dumps({"c": customer.id, "s": store.id}, salt=UNSUBSCRIBE_SALT)
    return store_url(store, reverse("customer-unsubscribe", args=[token]))


def read_unsubscribe(token):
    try:
        return signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None


def send_broadcast_batch(limit=BATCH):
    """Worker task: send up to `limit` pending broadcast emails. Returns how many were sent."""
    pending = (EmailBroadcastRecipient.objects.filter(sent_at__isnull=True, error="",
        broadcast__status__in=["queued", "sending"]).select_related("broadcast__store", "customer")
        .order_by("id")[:limit])
    sent = 0
    touched = set()
    for delivery in pending:
        broadcast, store = delivery.broadcast, delivery.broadcast.store
        touched.add(broadcast.id)
        customer = delivery.customer
        if customer is None or not customer.accepts_marketing:
            delivery.error = "berhenti berlangganan"
            delivery.save(update_fields=["error"])
            continue
        footer = (f"\n\n—\n{store.name} · {store_url(store)}\nTidak ingin menerima email promo lagi? "
            f"{unsubscribe_url(store, customer)}")
        try:
            send_mail(broadcast.subject, broadcast.body.replace("{nama}", customer.name or "") + footer,
                settings.DEFAULT_FROM_EMAIL, [delivery.email])
        except Exception as error:  # noqa: BLE001 - one bad address must not stop the rest
            logger.warning("broadcast %s to %s failed: %s", broadcast.id, delivery.email, error)
            delivery.error = str(error)[:200] or "gagal"
            delivery.save(update_fields=["error"])
            continue
        delivery.sent_at = timezone.now()
        delivery.save(update_fields=["sent_at"])
        sent += 1
    for broadcast_id in touched:
        _refresh(broadcast_id)
    return sent


def _refresh(broadcast_id):
    broadcast = EmailBroadcast.objects.get(id=broadcast_id)
    deliveries = broadcast.deliveries
    broadcast.sent_count = deliveries.filter(sent_at__isnull=False).count()
    broadcast.failed_count = deliveries.exclude(error="").count()
    done = not deliveries.filter(sent_at__isnull=True, error="").exists()
    broadcast.status = "sent" if done else "sending"
    if done and not broadcast.finished_at:
        broadcast.finished_at = timezone.now()
    broadcast.save(update_fields=["sent_count", "failed_count", "status", "finished_at"])
