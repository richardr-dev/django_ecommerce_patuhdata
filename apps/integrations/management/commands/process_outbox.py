import logging
import time
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.models import OutboxEvent, WorkerHeartbeat
from apps.orders.engagement import send_abandoned_cart_recoveries
from apps.orders.models import Order
from apps.orders.services import expire_pending_orders
from apps.orders.tracking import sync_active_shipments

logger = logging.getLogger("niagakit.worker")

# A claimed event whose worker died is picked up again after this long.
STALE_CLAIM = timedelta(minutes=10)


def _notification(event):
    order_id = event.payload.get("order_id")
    if not order_id:
        return {"skipped": "event has no order"}
    order = Order.objects.filter(id=order_id, store=event.store).first()
    if not order or not order.email:
        return {"skipped": "customer has no email"}
    if order.status == "cancelled" and not order.payment_checkout_url:
        return {"skipped": "payment session was never shown to the customer"}
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
        due = (Q(status__in={"pending", "failed"}) &
            (Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)))
        abandoned = Q(status="processing", last_attempt_at__lte=now - STALE_CLAIM)
        event = (OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(due | abandoned, attempts__lt=OutboxEvent.MAX_ATTEMPTS)
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
        log = logger.error if event.status == "dead" else logger.warning
        log("outbox event %s (%s) failed on attempt %s: %s", event.id, event.topic,
            event.attempts, error)
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
            try:
                summary = run_cycle(options["batch_size"])
            except Exception:  # e.g. database unreachable; keep the loop alive
                if not options["loop"]:
                    raise
                logger.exception("worker cycle failed; retrying in %ss", options["poll_seconds"] * 5)
                time.sleep(options["poll_seconds"] * 5)
                continue
            if not options["loop"]:
                self.stdout.write(
                    f"Processed {summary['processed']} outbox event(s); synced "
                    f"{summary['synced_shipments']} shipment(s); sent {summary['recoveries']} cart "
                    "recovery email(s).")
                return
            if not summary["processed"]:
                time.sleep(options["poll_seconds"])


def _run_task(name, task, errors):
    """Run one periodic task so its failure cannot stop the others or the loop."""
    try:
        return task()
    except Exception as error:  # isolate periodic tasks from each other
        logger.exception("worker task %s failed", name)
        errors.append(f"{name}: {error}")
        return 0


def run_cycle(batch_size):
    errors = []
    _run_task("expire_pending_orders", lambda: expire_pending_orders(limit=batch_size), errors)
    recoveries = _run_task("cart_recovery",
        lambda: send_abandoned_cart_recoveries(limit=batch_size), errors)
    synced_shipments = _run_task("shipment_sync",
        lambda: sync_active_shipments(limit=min(batch_size, 25)), errors)
    processed = 0
    while processed < batch_size and process_one_event():
        processed += 1
    WorkerHeartbeat.beat("process_outbox", error="; ".join(errors))
    return {"processed": processed, "recoveries": recoveries, "synced_shipments": synced_shipments}
