import time

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.models import OutboxEvent
from apps.orders.models import Order
from apps.orders.services import expire_pending_orders
from apps.orders.tracking import sync_active_shipments


def _notification(event):
    order_id = event.payload.get("order_id")
    if not order_id:
        return {"skipped": "event has no order"}
    order = Order.objects.filter(id=order_id, store=event.store).first()
    if not order or not order.email:
        return {"skipped": "customer has no email"}
    status = order.get_status_display()
    subject = f"{order.store.name}: pesanan {status.lower()}"
    url = f"{settings.PUBLIC_BASE_URL}/orders/{order.id}/?token={order.access_token}"
    sent = send_mail(subject,
        f"Pesanan {order.id} sekarang berstatus {status}. Detail: {url}",
        settings.DEFAULT_FROM_EMAIL, [order.email], fail_silently=False)
    return {"email_sent": bool(sent)}


HANDLED_TOPICS = {"order.created", "payment.received", "order.status_changed", "order.refunded"}


def process_one_event():
    now = timezone.now()
    with transaction.atomic():
        event = (OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(status__in={"pending", "failed"}, attempts__lt=8)
            .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
            .order_by("created_at").first())
        if not event:
            return False
        event.status = "processing"
        event.attempts += 1
        event.last_attempt_at = now
        event.save(update_fields=["status", "attempts", "last_attempt_at"])
    try:
        response = _notification(event) if event.topic in HANDLED_TOPICS else {
            "skipped": f"no handler for {event.topic}"
        }
    except Exception as error:  # noqa: BLE001 - worker boundary must persist every delivery failure
        event.schedule_retry(error)
        return True
    event.status = "succeeded"
    event.response = response
    event.processed_at = timezone.now()
    event.next_retry_at = None
    event.last_error = ""
    event.save(update_fields=["status", "response", "processed_at", "next_retry_at", "last_error"])
    return True


class Command(BaseCommand):
    help = "Deliver durable outbox notifications and expire unpaid orders"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--poll-seconds", type=float, default=2)

    def handle(self, *args, **options):
        while True:
            expire_pending_orders(limit=options["batch_size"])
            synced_shipments = sync_active_shipments(limit=min(options["batch_size"], 25))
            processed = 0
            while processed < options["batch_size"] and process_one_event():
                processed += 1
            if not options["loop"]:
                self.stdout.write(
                    f"Processed {processed} outbox event(s); synced {synced_shipments} shipment(s).")
                return
            if not processed:
                time.sleep(options["poll_seconds"])
