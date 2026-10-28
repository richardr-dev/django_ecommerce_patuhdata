import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.integrations.management.commands.process_outbox import process_one_event, run_cycle
from apps.integrations.models import OutboxEvent, WorkerHeartbeat
from apps.orders.engagement import send_abandoned_cart_recoveries
from apps.orders.models import CommerceSession, Order
from apps.orders.services import expire_pending_orders
from apps.orders.tracking import sync_active_shipments
from apps.shops.models import Store
from config.request_id import RequestIDFilter, RequestIDMiddleware


def _order(store, key, **fields):
    defaults = {"customer_name": "Ayu", "phone": "081234567890", "shipping_address": "Jakarta",
        "subtotal": Decimal(1000), "total": Decimal(1000), "idempotency_key": key}
    return Order.objects.create(store=store, **{**defaults, **fields})


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OutboxDeadLetterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ops-owner")
        self.store = Store.objects.create(owner=self.owner, name="Ops", slug="ops")
        self.order = _order(self.store, "ops-order", email="ayu@example.com")

    def _event(self, **fields):
        return OutboxEvent.objects.create(store=self.store, topic="order.created",
            aggregate_id=str(self.order.id), payload={"order_id": str(self.order.id)}, **fields)

    @patch("apps.integrations.management.commands.process_outbox.send_mail",
        side_effect=ConnectionRefusedError("smtp down"))
    def test_event_becomes_dead_after_last_attempt(self, _send):
        event = self._event(attempts=OutboxEvent.MAX_ATTEMPTS - 1)
        process_one_event()
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts), ("dead", OutboxEvent.MAX_ATTEMPTS))
        self.assertIsNone(event.next_retry_at)
        self.assertIn("smtp down", event.last_error)
        self.assertFalse(process_one_event(), "dead events are not retried automatically")

    def test_merchant_retry_revives_a_dead_event(self):
        event = self._event(status="dead", attempts=OutboxEvent.MAX_ATTEMPTS, last_error="smtp down")
        client = Client()
        client.force_login(self.owner)
        page = client.get("/merchant/integrations/")
        self.assertContains(page, f"/merchant/integrations/{event.id}/retry/")

        client.post(f"/merchant/integrations/{event.id}/retry/")
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts), ("pending", 0))
        self.assertTrue(process_one_event())
        event.refresh_from_db()
        self.assertEqual(event.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)

    def test_event_claimed_by_a_crashed_worker_is_picked_up_again(self):
        fresh = self._event(status="processing", attempts=1, last_attempt_at=timezone.now())
        self.assertFalse(process_one_event(), "a recent claim belongs to a live worker")
        OutboxEvent.objects.filter(id=fresh.id).update(
            last_attempt_at=timezone.now() - timedelta(minutes=11))
        self.assertTrue(process_one_event())
        fresh.refresh_from_db()
        self.assertEqual((fresh.status, fresh.attempts), ("succeeded", 2))


class HealthCheckTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("health-owner")
        self.store = Store.objects.create(owner=owner, name="Health", slug="health")

    def test_background_problems_alert_without_failing_liveness(self):
        OutboxEvent.objects.create(store=self.store, topic="order.created", aggregate_id="x",
            status="dead", attempts=8)
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["outbox_dead_events"], 1)
        self.assertEqual(sorted(body["alerts"]), ["outbox_dead_events", "worker_stale"])

    def test_fresh_worker_heartbeat_clears_the_stale_alert(self):
        WorkerHeartbeat.beat("process_outbox")
        body = self.client.get("/healthz/").json()
        self.assertEqual(body["alerts"], [])
        self.assertLessEqual(body["worker_last_seen_seconds"], 1)

    @override_settings(ALLOWED_HOSTS=["shop.example.com"])
    def test_load_balancer_probe_by_ip_is_answered(self):
        response = self.client.get("/healthz/", HTTP_HOST="10.0.3.17:8000")
        self.assertEqual(response.status_code, 200)

    def test_database_failure_is_still_503(self):
        with patch("config.views.connection.cursor", side_effect=RuntimeError("db down")):
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)


class WorkerIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("iso-owner")
        self.store = Store.objects.create(owner=owner, name="Iso", slug="iso")

    @override_settings(DEBUG=False, ADMINS=[("Ops", "ops@example.com")],
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("apps.integrations.management.commands.process_outbox.expire_pending_orders",
        side_effect=RuntimeError("expiry bug"))
    def test_failing_task_does_not_stop_the_cycle_and_alerts_admins(self, _expire):
        event = OutboxEvent.objects.create(store=self.store, topic="unknown.topic", aggregate_id="x")
        summary = run_cycle(batch_size=10)
        event.refresh_from_db()
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(event.status, "succeeded")
        heartbeat = WorkerHeartbeat.objects.get(name="process_outbox")
        self.assertIn("expiry bug", heartbeat.last_error)
        self.assertTrue(any("expire_pending_orders" in message.subject + message.body
            for message in mail.outbox))

    def test_one_failing_order_does_not_block_other_expiries(self):
        past = timezone.now() - timedelta(minutes=1)
        broken = _order(self.store, "broken", payment_expires_at=past - timedelta(minutes=1))
        healthy = _order(self.store, "healthy", payment_expires_at=past)
        real = __import__("apps.orders.services", fromlist=["_gateway_status"])._gateway_status

        def gateway_status(order, now):
            if order.id == broken.id:
                raise KeyError("malformed gateway payload")
            return real(order, now)

        with patch("apps.orders.services._gateway_status", gateway_status), \
                self.assertLogs("niagakit.orders", "ERROR"):
            self.assertEqual(expire_pending_orders(), 1)
        healthy.refresh_from_db()
        broken.refresh_from_db()
        self.assertEqual((healthy.status, broken.status), ("cancelled", "pending"))

    def test_malformed_courier_response_is_recorded_and_batch_continues(self):
        first = _order(self.store, "track-1", status="shipped", tracking_number="A1", courier_code="jne")
        second = _order(self.store, "track-2", status="shipped", tracking_number="B2", courier_code="jne")

        def sync(order):
            if order.id == first.id:
                raise AttributeError("'NoneType' object has no attribute 'get'")

        with patch("apps.orders.tracking.sync_order_tracking", side_effect=sync), \
                self.assertLogs("niagakit.shipping", "ERROR"):
            self.assertEqual(sync_active_shipments(), 1)
        first.refresh_from_db()
        self.assertIn("NoneType", first.tracking_error)
        self.assertIsNotNone(first.tracking_last_checked_at)
        second.refresh_from_db()
        self.assertEqual(second.tracking_error, "")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_smtp_failure_skips_one_cart_recovery_and_releases_its_claim(self):
        idle = timezone.now() - timedelta(hours=1)
        sessions = [CommerceSession.objects.create(store=self.store, cart_item_count=1,
            recovery_email=f"buyer{index}@example.com", recovery_consent_at=idle)
            for index in range(2)]
        CommerceSession.objects.update(last_seen_at=idle)
        real_send = __import__("django.core.mail", fromlist=["send_mail"]).send_mail

        def send(subject, body, sender, recipients, **kwargs):
            if recipients == ["buyer0@example.com"]:
                raise ConnectionRefusedError("smtp down")
            return real_send(subject, body, sender, recipients, **kwargs)

        with patch("apps.orders.engagement.send_mail", send), \
                self.assertLogs("niagakit.engagement", "ERROR"):
            self.assertEqual(send_abandoned_cart_recoveries(), 1)
        sessions[0].refresh_from_db()
        sessions[1].refresh_from_db()
        self.assertIsNone(sessions[0].recovery_sent_at, "failed send is retried next cycle")
        self.assertIsNotNone(sessions[1].recovery_sent_at)


class RequestIdTests(TestCase):
    def _run(self, header=None):
        records = []
        handler = logging.Handler()
        handler.addFilter(RequestIDFilter())
        handler.emit = records.append
        logger = logging.getLogger("niagakit.test-request-id")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        def view(request):
            logger.warning("inside view")
            from django.http import HttpResponse
            return HttpResponse("ok")

        headers = {"HTTP_X_REQUEST_ID": header} if header else {}
        response = RequestIDMiddleware(view)(RequestFactory().get("/", **headers))
        return response, records[0].request_id

    def test_generated_id_is_returned_and_logged(self):
        response, logged = self._run()
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")
        self.assertEqual(logged, response["X-Request-ID"])

    def test_safe_upstream_id_is_kept(self):
        response, logged = self._run("Root-1.abc:def_12345")
        self.assertEqual((response["X-Request-ID"], logged), ("Root-1.abc:def_12345",) * 2)

    def test_unsafe_upstream_id_is_replaced(self):
        response, _ = self._run("evil\nINJECTED log line")
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_logs_outside_requests_use_a_placeholder(self):
        record = logging.LogRecord("x", logging.INFO, "", 0, "msg", (), None)
        RequestIDFilter().filter(record)
        self.assertEqual(record.request_id, "-")

    def test_real_responses_carry_the_header(self):
        self.assertIn("X-Request-ID", self.client.get("/healthz/"))
