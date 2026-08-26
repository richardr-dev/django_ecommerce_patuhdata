from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.integrations.models import OutboxEvent
from apps.orders.models import Order, PaymentTransaction
from apps.orders.refunds import apply_refund_result, create_refund_request
from apps.orders.services import transition_order
from apps.shops.models import Store


class CommerceReliabilityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("reliability-owner")
        self.store = Store.objects.create(owner=self.owner, name="Reliable", slug="reliable")
        self.order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal("100.00"), total=Decimal("100.00"),
            idempotency_key="reliability-order")

    def test_customer_order_requires_session_or_access_token(self):
        stranger = Client()
        denied = stranger.get(f"/orders/{self.order.id}/")
        allowed = stranger.get(f"/orders/{self.order.id}/", {"token": self.order.access_token})

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)

    def test_financial_database_constraints_reject_negative_order_total(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(store=self.store, customer_name="Bad", phone="0812",
                shipping_address="Jakarta", subtotal=Decimal("1.00"), total=Decimal("-1.00"),
                idempotency_key="negative-order")

    def test_multiple_partial_refunds_can_reach_fully_refunded(self):
        transition_order(self.order, "paid")
        payment = PaymentTransaction.objects.create(order=self.order, provider="demo",
            external_id="paid-1", status="succeeded", amount=self.order.total, reconciled=True)

        first = create_refund_request(order=self.order, amount=40, reason="OTHERS",
            actor=self.owner)
        apply_refund_result(first, status="succeeded", reference="refund-1", payload={})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "partially_refunded")

        second = create_refund_request(order=self.order, amount=60, reason="OTHERS",
            actor=self.owner)
        apply_refund_result(second, status="succeeded", reference="refund-2", payload={})
        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")
        self.assertEqual(self.order.refunds.filter(status="succeeded").count(), 2)

    def test_transition_uses_current_database_state_not_stale_instance(self):
        stale = Order.objects.get(pk=self.order.pk)
        transition_order(self.order, "paid")
        transition_order(stale, "confirmed")
        stale.refresh_from_db()
        self.assertEqual(stale.status, "confirmed")

    def test_expired_payment_releases_order_and_is_not_fulfilled(self):
        self.order.payment_expires_at = timezone.now()
        self.order.save(update_fields=["payment_expires_at"])
        call_command("process_outbox")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_outbox_worker_delivers_customer_notification_once(self):
        self.order.email = "ayu@example.com"
        self.order.save(update_fields=["email"])
        event = OutboxEvent.objects.create(store=self.store, topic="order.created",
            aggregate_id=str(self.order.id), payload={"order_id": str(self.order.id)})

        call_command("process_outbox")
        call_command("process_outbox")

        event.refresh_from_db()
        self.assertEqual(event.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(self.order.access_token), mail.outbox[0].body)

    def test_merchant_cannot_bypass_verified_refund_workflow(self):
        transition_order(self.order, "paid")
        client = Client()
        client.force_login(self.owner)
        client.post(f"/merchant/orders/{self.order.id}/update/", {"status": "refunded"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "paid")
        self.assertFalse(self.order.refunds.exists())

    def test_shipping_requires_tracking_number(self):
        for status in ("paid", "confirmed", "allocated", "picking", "packed"):
            transition_order(self.order, status)
        client = Client()
        client.force_login(self.owner)
        client.post(f"/merchant/orders/{self.order.id}/update/", {"status": "shipped"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "packed")

    def test_backend_dashboard_metrics_and_alerts_are_tenant_scoped(self):
        transition_order(self.order, "paid")
        other = Store.objects.create(owner=self.owner, name="Other", slug="dashboard-other")
        Order.objects.create(store=other, customer_name="Other Customer", phone="0899",
            shipping_address="Bandung", subtotal=Decimal("9999.00"), total=Decimal("9999.00"),
            status="paid", idempotency_key="other-dashboard-order")
        PaymentTransaction.objects.create(order=self.order, provider="demo",
            external_id="dashboard-failure", status="failed", amount=self.order.total,
            reconciled=False)
        client = Client()
        client.force_login(self.owner)

        response = client.get("/merchant/", {"store": self.store.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["today_gross"], Decimal("100.00"))
        self.assertEqual(response.context["today_orders"], 1)
        self.assertEqual(response.context["fulfillment"]["paid"], 1)
        self.assertEqual(response.context["payment_errors"], 1)
        self.assertContains(response, "System health")
        self.assertNotContains(response, "9999")
