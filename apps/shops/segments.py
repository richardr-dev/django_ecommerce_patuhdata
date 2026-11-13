"""Segmentasi pelanggan and email broadcast.

A segment is a set of filters over the store's customers, with order stats computed from
their real (paid, not cancelled) orders, matched by WhatsApp number. A broadcast emails one
segment; only customers who agreed to promotions get it, each with an unsubscribe link."""
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import (
    Count,
    DecimalField,
    Exists,
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

    # FK-based: reliable once CRM sync has run
    fk_qs = Order.objects.filter(
        store=OuterRef("store"), customer=OuterRef("pk")
    ).exclude(status__in=UNSOLD).values("customer")

    # Phone-based fallback for orders not yet synced
    phone_qs = Order.objects.filter(
        store=OuterRef("store"), phone=OuterRef("phone")
    ).exclude(phone="").exclude(status__in=UNSOLD).values("phone")

    return queryset.annotate(
        order_count=Coalesce(
            Subquery(fk_qs.annotate(n=Count("id")).values("n")),
            Subquery(phone_qs.annotate(n=Count("id")).values("n")),
            Value(0),
            output_field=IntegerField(),
        ),
        total_spent=Coalesce(
            Subquery(fk_qs.annotate(t=Sum("total")).values("t")),
            Subquery(phone_qs.annotate(t=Sum("total")).values("t")),
            Value(Decimal(0)),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        last_order_at=Coalesce(
            Subquery(fk_qs.annotate(last=Max("created_at")).values("last")),
            Subquery(phone_qs.annotate(last=Max("created_at")).values("last")),
        ),
    )


def _int(value):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse(params):
    """The segment filters from a GET/POST dict, cleaned."""
    segment = {}
    for key in ("tag", "q", "location"):
        value = (params.get(key) or "").strip()[:60]
        if value:
            segment[key] = value
    from apps.orders.models import Order
    known = {code for code, _ in Order.CHANNELS}
    raw = params.getlist("channel") if hasattr(params, "getlist") else [params.get("channel") or ""]
    channels = [c for item in raw for c in str(item or "").split(",") if c.strip() in known]
    if channels:
        segment["channel"] = ",".join(dict.fromkeys(c.strip() for c in channels))
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
    if segment.get("channel"):
        from apps.orders.models import Order
        names = dict(Order.CHANNELS)
        parts.append("beli di " + " atau ".join(names.get(c, c) for c in segment["channel"].split(",")))
    if segment.get("location"):
        parts.append(f"lokasi \"{segment['location']}\"")
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
    if segment.get("channel"):
        customers = customers.filter(by_channel(segment["channel"].split(",")))
    for word in (segment.get("location") or "").replace(",", " ").split():
        customers = customers.filter(by_location(word))
    return customers


def by_channel(channels):
    """Customers who bought on any of these channels: an order of theirs, or a marketplace buyer."""
    from apps.orders.models import Order
    bought = Order.objects.filter(store=OuterRef("store"), customer=OuterRef("pk"),
        channel__in=channels).exclude(status__in=["cancelled", "payment_failed"])
    return Q(Exists(bought)) | Q(channel_buyer__platform__in=channels)


def by_location(word):
    """One word of a place (city, province, district or any part of the address). Every word typed
    must match somewhere, so "bogor jawa barat" finds Kab. Bogor, Jawa Barat."""
    from apps.orders.models import Order
    near = Order.objects.filter(store=OuterRef("store"), customer=OuterRef("pk")).filter(
        Q(city__icontains=word) | Q(province__icontains=word) | Q(district__icontains=word)
        | Q(shipping_address__icontains=word))
    return (Q(address__icontains=word) | Q(channel_buyer__city__icontains=word)
        | Q(channel_buyer__province__icontains=word) | Q(Exists(near)))


def places(store, customers):
    """For a page of customers: where they are and where they bought, keyed by customer id.
    ``where`` is the readable place; ``city`` and ``province`` are kept apart for exports."""
    from apps.orders.models import Order
    ids = [c.pk for c in customers]
    found = {}
    channels = defaultdict(list)
    for customer in customers:
        buyer = customer.channel_buyer
        if buyer and (buyer.city or buyer.province):
            found[customer.pk] = (buyer.city, buyer.province)
    names = dict(Order.CHANNELS)
    rows = Order.objects.filter(store=store, customer_id__in=ids).exclude(
        status__in=["cancelled", "payment_failed"]).order_by("-created_at").values(
        "customer_id", "channel", "city", "province")
    for row in rows:
        if row["channel"] not in channels[row["customer_id"]]:
            channels[row["customer_id"]].append(row["channel"])
        if row["customer_id"] not in found and (row["city"] or row["province"]):
            found[row["customer_id"]] = (row["city"], row["province"])
    out = {}
    for customer in customers:
        city, province = found.get(customer.pk, ("", ""))
        where = ", ".join(x for x in (city, province) if x)
        if not where and customer.address.strip():
            where = customer.address.strip().replace("\n", " ")[:60]
        if customer.channel_buyer_id and customer.channel_buyer.platform not in channels[customer.pk]:
            channels[customer.pk].append(customer.channel_buyer.platform)
        out[customer.pk] = {"where": where, "city": city, "province": province,
            "channels": [(c, names.get(c, c)) for c in channels[customer.pk]]}
    return out


def location_facets(store, limit=10):
    """The provinces customers are in, biggest first, for one-click filters."""
    from apps.channels.models import ChannelBuyer
    rows = (ChannelBuyer.objects.filter(store=store).exclude(province="").values("province")
        .annotate(n=Count("id")).order_by("-n")[:limit])
    return [(row["province"], row["n"]) for row in rows]


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
