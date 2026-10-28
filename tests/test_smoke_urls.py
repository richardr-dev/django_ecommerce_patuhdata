"""Every route must answer every role and junk input without a server error.

Routes are discovered from the URL resolver, so new pages are covered automatically. Add sample
values to URL_KWARGS when a route introduces a new path parameter.
"""
from decimal import Decimal
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.catalog.models import Category, InventoryLevel, Product, ProductVariant, Warehouse
from apps.integrations.models import OutboxEvent
from apps.orders.models import Coupon, Order, OrderItem, PaymentTransaction, ShippingMethod
from apps.orders.services import transition_order
from apps.orders.views import BACKOFFICE
from apps.shops.models import Customer, Store, StoreStaff

SKIPPED_PREFIXES = ("platform-admin/", "media/", "static/")

JUNK_QUERY = {"q": "'\"<script>%00", "status": "bogus", "category": "x", "sort": "x", "page": "-1",
    "token": "not-a-uuid", "next": "https://evil.example.com/"}

JUNK_POST = {"quantity": "abc", "amount": "-1e999", "variant_id": "x", "status": "bogus",
    "phone": "abc", "postal_code": "!!", "destination_area_id": "../..", "shipping_method": "x",
    "payment_method": "x", "coupon_code": "'; DROP TABLE", "loyalty_points": "NaN",
    "customer_name": "x" * 500, "shipping_address": "", "email": "not-email",
    "tracking_number": "<b>", "courier_code": "JNE!", "fulfillment_method": "x",
    "method": "manual_transfer", "transfer_reference": "", "reason": "bogus",
    "recovery_email": "x", "recovery_consent": "on", "idempotency_key": "x" * 200,
    "on_hand": "-5", "safety_stock": "x", "price": "abc", "sku": "", "name": "",
    "username": "x", "password": "x", "csv_file": ""}


def _routes(resolver=None, prefix=""):
    resolver = resolver or get_resolver()
    for pattern in resolver.url_patterns:
        route = prefix + str(pattern.pattern)
        if route.startswith(SKIPPED_PREFIXES):
            continue
        if isinstance(pattern, URLResolver):
            yield from _routes(pattern, route)
        elif isinstance(pattern, URLPattern):
            yield route, pattern


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="",
    RAJAONGKIR_API_KEY="", DOKU_CLIENT_ID="", XENDIT_SECRET_KEY="")
class SmokeUrlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("smoke-owner", password="Owner-Pass-2026!")
        cls.customer = User.objects.create_user("smoke-customer", email="c@example.com")
        cls.viewer = User.objects.create_user("smoke-viewer")
        cls.store = Store.objects.create(owner=cls.owner, name="Smoke", slug="smoke",
            support_phone="081200000000")
        StoreStaff.objects.create(store=cls.store, user=cls.viewer, role="viewer")
        category = Category.objects.create(store=cls.store, name="Kopi", slug="kopi")
        cls.product = Product.objects.create(store=cls.store, category=category, name="Gayo",
            slug="gayo", sku="GAYO", price=50000, weight_grams=250)
        cls.variant = ProductVariant.objects.create(product=cls.product, name="500g",
            sku="GAYO-500", stock=5)
        warehouse = Warehouse.objects.create(store=cls.store, name="Jakarta", code="JKT")
        cls.level = InventoryLevel.objects.create(warehouse=warehouse, product=cls.product, on_hand=5)
        ShippingMethod.objects.create(store=cls.store, code="reg", name="Reguler", flat_rate=10000)
        Coupon.objects.create(store=cls.store, code="HEMAT", kind="percent", value=10)
        cls.customer_record = Customer.objects.create(store=cls.store, name="Ayu",
            phone="081234567890")
        cls.order = Order.objects.create(store=cls.store, customer_user=cls.customer,
            customer_name="Ayu", phone="081234567890", shipping_address="Jakarta",
            subtotal=Decimal(50000), total=Decimal(50000), idempotency_key="smoke-order",
            payment_reference="demo-smoke")
        OrderItem.objects.create(order=cls.order, product=cls.product, name="Gayo", sku="GAYO",
            unit_price=50000, quantity=1, line_total=50000)
        PaymentTransaction.objects.create(order=cls.order, provider="demo",
            external_id="demo-smoke", status="succeeded", reconciled=True, amount=Decimal(50000))
        transition_order(cls.order, "paid")
        cls.event = OutboxEvent.objects.create(store=cls.store, topic="order.created",
            aggregate_id=str(cls.order.id), payload={"order_id": str(cls.order.id)})

    def url_kwargs(self):
        return {"order_id": self.order.id, "product_id": self.product.id, "slug": self.product.slug,
            "item_key": f"p:{self.product.id}", "token": self.order.access_token,
            "customer_id": self.customer_record.id, "event_id": self.event.id,
            "section": "products", "object_id": self.product.id, "uidb64": "MQ"}

    def _clients(self):
        anonymous = Client(raise_request_exception=False)
        customer = Client(raise_request_exception=False)
        customer.force_login(self.customer)
        owner = Client(raise_request_exception=False)
        owner.force_login(self.owner)
        viewer = Client(raise_request_exception=False)
        viewer.force_login(self.viewer)
        for client in (anonymous, customer):
            client.post(f"/cart/add/{self.product.id}/", {"variant_id": self.variant.id})
        return {"anonymous": anonymous, "customer": customer, "owner": owner, "viewer": viewer}

    def _urls(self):
        samples = self.url_kwargs()
        for _route, pattern in _routes():
            names = list(pattern.pattern.converters)
            kwargs = {name: samples[name] for name in names}
            if pattern.name == "password-reset-confirm":
                kwargs["token"] = "set-password"
            path = reverse(pattern.name, kwargs=kwargs)
            yield pattern.name, path
            if "section" in names:
                for section in BACKOFFICE:
                    other = {**kwargs, "section": section}
                    if "object_id" in other:
                        model = BACKOFFICE[section][0]
                        first = model.objects.order_by("id").first()
                        if not first:
                            continue
                        other["object_id"] = first.id
                    yield pattern.name, reverse(pattern.name, kwargs=other)

    @patch("apps.integrations.http.requests.request",
        side_effect=requests.ConnectionError("network disabled in tests"))
    def test_no_route_returns_a_server_error(self, _network):
        failures = []
        clients = self._clients()
        urls = list(self._urls())
        self.assertGreater(len(urls), 40)
        for role, client in clients.items():
            for name, path in urls:
                for method, data in (("get", None), ("get", JUNK_QUERY), ("post", {}),
                        ("post", JUNK_POST)):
                    response = getattr(client, method)(path, data or {})
                    if response.status_code >= 500 and response.status_code not in {502, 503}:
                        failures.append(f"{role} {method.upper()} {path} {data and 'junk' or ''}"
                            f" -> {response.status_code}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))


class CartInputTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("cart-input-owner")
        self.store = Store.objects.create(owner=owner, name="Cart Input", slug="cart-input")
        self.product = Product.objects.create(store=self.store, name="Kaos", slug="kaos", sku="K",
            price=1000)
        warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.product,
            on_hand=0)
        self.client = Client()

    def test_variant_product_without_variant_choice_asks_for_one(self):
        ProductVariant.objects.create(product=self.product, name="M", sku="K-M", stock=3)
        for variant_id in ("", "x", "999999"):
            response = self.client.post(f"/cart/add/{self.product.id}/", {"variant_id": variant_id})
            self.assertRedirects(response, "/products/kaos/", fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_out_of_stock_product_is_not_reported_as_added(self):
        response = self.client.post(f"/cart/add/{self.product.id}/", follow=True)
        self.assertContains(response, "sedang habis")
        self.assertNotIn(f"p:{self.product.id}", self.client.session.get("cart", {}))

    def test_corrupt_cart_session_keys_are_ignored(self):
        self.level.on_hand = 2
        self.level.save()
        session = self.client.session
        session["cart"] = {"p:abc": 1, "v:": 1, "zzz": "x", f"p:{self.product.id}": "nope",
            str(self.product.id): 1}
        session.save()
        response = self.client.get("/cart/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kaos")
