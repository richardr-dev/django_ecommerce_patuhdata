import hashlib
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import LoyaltyAccount, MobileAccessToken, Order, OrderItem
from apps.shops.models import MarketingCampaign, Store


class MobileApiTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("api-owner")
        self.user = User.objects.create_user(
            "buyer@example.com",
            email="buyer@example.com",
            password="safe-password",
            first_name="Ayu",
        )
        self.store = Store.objects.create(
            owner=owner,
            name="Mobile Shop",
            slug="mobile-shop",
            loyalty_enabled=True,
            loyalty_point_value=100,
            loyalty_min_redeem=10,
        )
        self.other_store = Store.objects.create(owner=owner, name="Other Shop", slug="other-shop")
        self.product = Product.objects.create(
            store=self.store,
            name="Mobile Product",
            slug="mobile-product",
            sku="MOBILE-1",
            price=125000,
        )
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=7)
        self.headers = {"HTTP_X_STORE_SLUG": self.store.slug}

    def login(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            json.dumps({"email": "buyer@example.com", "password": "safe-password"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201)
        raw = response.json()["data"]["access_token"]
        return raw, {**self.headers, "HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def test_store_and_catalog_are_available_without_login(self):
        campaign = MarketingCampaign.objects.create(
            store=self.store,
            name="Mobile Sale",
            headline="App-only deal",
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        response = self.client.get("/api/v1/store/", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["campaign"]["id"], campaign.id)
        products = self.client.get("/api/v1/products/?page_size=1", **self.headers).json()
        self.assertEqual(products["meta"]["total"], 1)
        self.assertEqual(products["data"][0]["available_stock"], 7)

    def test_raw_access_token_is_not_stored(self):
        raw, _ = self.login()
        token = MobileAccessToken.objects.get()
        self.assertNotEqual(token.token_hash, raw)
        self.assertEqual(token.token_hash, hashlib.sha256(raw.encode()).hexdigest())

    def test_authenticated_orders_and_loyalty_are_store_scoped(self):
        order = Order.objects.create(
            store=self.store,
            customer_user=self.user,
            customer_name="Ayu",
            email=self.user.email,
            phone="081234567890",
            shipping_address="Jakarta",
            subtotal=125000,
            total=125000,
            idempotency_key="mobile-order",
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            name=self.product.name,
            sku=self.product.sku,
            unit_price=125000,
            quantity=1,
            line_total=125000,
        )
        Order.objects.create(
            store=self.other_store,
            customer_user=self.user,
            customer_name="Ayu",
            email=self.user.email,
            phone="081234567890",
            shipping_address="Jakarta",
            subtotal=1,
            total=1,
            idempotency_key="other-order",
        )
        LoyaltyAccount.objects.create(
            store=self.store, user=self.user, balance=42, lifetime_earned=50
        )
        _, authenticated = self.login()
        orders = self.client.get("/api/v1/orders/", **authenticated).json()["data"]
        self.assertEqual([row["id"] for row in orders], [str(order.id)])
        wallet = self.client.get("/api/v1/loyalty/", **authenticated).json()["data"]
        self.assertEqual(wallet["balance"], 42)

    def test_token_cannot_cross_store_and_logout_revokes_it(self):
        _, authenticated = self.login()
        cross_store = {**authenticated, "HTTP_X_STORE_SLUG": self.other_store.slug}
        self.assertEqual(self.client.get("/api/v1/me/", **cross_store).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout/", content_type="application/json", **authenticated
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/v1/me/", **authenticated).status_code, 401)

    def test_invalid_json_and_missing_store_have_stable_errors(self):
        bad = self.client.post(
            "/api/v1/auth/token/", "not-json", content_type="application/json", **self.headers
        )
        self.assertEqual(bad.json()["error"]["code"], "invalid_request")
        missing = self.client.get("/api/v1/store/", HTTP_X_STORE_SLUG="does-not-exist")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "store_not_found")
