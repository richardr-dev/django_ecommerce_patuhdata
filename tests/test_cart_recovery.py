from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.engagement import send_abandoned_cart_recoveries
from apps.orders.models import CommerceSession
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PUBLIC_BASE_URL="https://shop.example")
class CartRecoveryTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("recovery-owner")
        self.buyer = User.objects.create_user("recovery-buyer", email="buyer@example.com",
            password="test")
        self.store = Store.objects.create(owner=owner, name="Recovery Shop", slug="recovery-shop")
        self.product = Product.objects.create(store=self.store, name="Recovery Product",
            slug="recovery-product", sku="RECOVERY-1", price=85000)
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=10)
        self.client.force_login(self.buyer)

    def test_opted_in_abandoned_cart_sends_once_and_link_restores_cart(self):
        self.client.post(f"/cart/add/{self.product.id}/", {"quantity": 2})
        response = self.client.post("/cart/recovery/", {"recovery_email": "buyer@example.com",
            "recovery_consent": "on"})
        self.assertRedirects(response, "/cart/")
        session = CommerceSession.objects.get(store=self.store)
        CommerceSession.objects.filter(id=session.id).update(
            last_seen_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(send_abandoned_cart_recoveries(), 1)
        self.assertEqual(send_abandoned_cart_recoveries(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(session.recovery_token), mail.outbox[0].body)

        browser_session = self.client.session
        browser_session["cart"] = {}
        browser_session.save()
        response = self.client.get(f"/cart/recover/{session.recovery_token}/")
        self.assertRedirects(response, "/cart/?store=recovery-shop")
        restored = self.client.get(response.url)
        self.assertContains(restored, "Recovery Product")
        self.assertEqual(self.client.session["cart"][f"p:{self.product.id}"], 2)

    def test_no_consent_means_no_recovery_email(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        session = CommerceSession.objects.get(store=self.store)
        CommerceSession.objects.filter(id=session.id).update(
            last_seen_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(send_abandoned_cart_recoveries(), 0)
        self.assertEqual(mail.outbox, [])

    def test_variant_identity_survives_recovery(self):
        variant = ProductVariant.objects.create(product=self.product, name="Orange / M",
            sku="RECOVERY-ORANGE-M", price=90000, stock=4)
        self.client.post(f"/cart/add/{self.product.id}/", {"variant_id": variant.id})
        self.client.post("/cart/recovery/", {"recovery_email": "buyer@example.com",
            "recovery_consent": "on"})
        session = CommerceSession.objects.get(store=self.store)
        browser_session = self.client.session
        browser_session["cart"] = {}
        browser_session.save()
        self.client.get(f"/cart/recover/{session.recovery_token}/")
        self.assertEqual(self.client.session["cart"][f"v:{variant.id}"], 1)

    def test_shopper_can_revoke_recovery_consent(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/cart/recovery/", {"recovery_email": "buyer@example.com",
            "recovery_consent": "on"})
        self.client.post("/cart/recovery/", {"recovery_email": "buyer@example.com"})
        session = CommerceSession.objects.get(store=self.store)
        self.assertEqual(session.recovery_email, "")
        self.assertIsNone(session.recovery_consent_at)
