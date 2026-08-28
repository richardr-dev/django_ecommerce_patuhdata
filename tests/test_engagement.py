from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.engagement import engagement_summary
from apps.orders.models import CommerceSession, JourneyEvent, Order
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class EngagementTrackingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("engagement-owner")
        self.buyer = User.objects.create_user("engagement-buyer", password="test")
        self.store = Store.objects.create(owner=self.owner, name="Engagement Shop", slug="engagement-shop")
        self.product = Product.objects.create(store=self.store, name="Interesting Product",
            slug="interesting-product", sku="INTEREST-1", price=125000)
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=10)
        self.client.force_login(self.buyer)

    def test_cart_snapshot_becomes_abandoned_and_appears_on_dashboard(self):
        self.client.post(f"/cart/add/{self.product.id}/", {"quantity": 2})
        session = CommerceSession.objects.get(store=self.store)
        self.assertEqual(session.cart_item_count, 2)
        self.assertEqual(session.cart_value, 250000)
        self.assertEqual(session.cart_snapshot[0]["name"], self.product.name)
        CommerceSession.objects.filter(id=session.id).update(
            last_seen_at=timezone.now() - timedelta(hours=1))
        self.client.force_login(self.owner)
        dashboard = self.client.get("/merchant/")
        self.assertContains(dashboard, "Abandoned cart")
        self.assertContains(dashboard, "250.000")
        detail = self.client.get("/merchant/engagement/")
        self.assertContains(detail, "Interesting Product")

    def test_high_intent_browsing_requires_analytics_consent(self):
        for _ in range(3):
            self.client.get(f"/products/{self.product.slug}/")
        self.assertFalse(JourneyEvent.objects.filter(kind="product_view").exists())
        self.client.cookies["cookie_consent"] = "all"
        for _ in range(3):
            self.client.get(f"/products/{self.product.slug}/")
        session = CommerceSession.objects.get(store=self.store)
        self.assertEqual(session.product_view_count, 3)
        self.assertEqual(engagement_summary(self.store)["engaged_count"], 1)

    def test_checkout_marks_session_converted_and_clears_snapshot(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.get("/checkout/")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        session = CommerceSession.objects.get(store=self.store)
        self.assertIsNotNone(session.converted_at)
        self.assertEqual(session.cart_item_count, 0)
        self.assertEqual(session.cart_snapshot, [])
        self.assertEqual(Order.objects.count(), 1)
        self.assertGreaterEqual(session.checkout_started_count, 1)
