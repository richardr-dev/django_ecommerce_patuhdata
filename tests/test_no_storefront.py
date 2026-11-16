"""STOREFRONT_ENABLED=0 (the default outside tests): no public online shop. Shopping pages are gone,
the back office, buyers' existing documents and payment callbacks are not."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import Product
from apps.orders.models import Order
from apps.orders.templatetags.merchant_nav import build_nav
from apps.shops.models import Store


class NoStorefrontTests(TestCase):
    def setUp(self):
        off = self.settings(STOREFRONT_ENABLED=False)  # in setUp: conftest switches it on for every test
        off.enable()
        self.addCleanup(off.disable)
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="K", price=50000)

    def test_shopping_pages_are_gone(self):
        shopper = Client()
        for path in ("/products/kopi/", "/cart/", "/checkout/", "/account/orders/", "/sitemap.xml",
                "/feeds/products.xml", "/api/v1/products/"):
            reply = shopper.get(path)
            self.assertEqual(reply.status_code, 410, path)
            self.assertContains(reply, "sudah tidak tersedia", status_code=410)
        self.assertEqual(shopper.post("/cart/add/1/").status_code, 410)

    def test_home_and_shopper_sign_in_lead_to_the_back_office_sign_in(self):
        shopper = Client()
        self.assertRedirects(shopper.get("/"), "/login/", fetch_redirect_response=False)
        self.assertRedirects(shopper.get("/account/login/"), "/login/", fetch_redirect_response=False)
        self.assertContains(shopper.get("/account/register/"), "tim sales")  # no shopper sign-up
        self.assertEqual(shopper.get("/login/").status_code, 200)
        self.assertEqual(shopper.get("/account/password-reset/").status_code, 200)
        staff = Client()
        staff.force_login(self.owner)
        self.assertRedirects(staff.get("/"), "/books/", fetch_redirect_response=False)

    def test_an_existing_order_stays_reachable_for_its_buyer(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="1", shipping_address="x",
            subtotal=Decimal(1), total=Decimal(1), payment_provider="bank_transfer", idempotency_key="k")
        self.assertEqual(Client().get(f"/orders/{order.id}/?token={order.access_token}").status_code, 200)

    def test_the_back_office_works_and_hides_the_online_shop_menus(self):
        staff = Client()
        staff.force_login(self.owner)
        page = staff.get("/books/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Lihat toko online")
        groups = {group["label"] for section in build_nav({"merchant_role": "admin", "merchant_store": self.store})
            for group in section["groups"]}
        self.assertNotIn("Toko online", groups)
        self.assertNotIn("Promosi", groups)
        self.assertIn("Marketplace", groups)
        self.assertContains(staff.get("/books/toko-online/"), "tidak diaktifkan di sistem ini")

    def test_switched_on_the_shop_is_served_again(self):
        with override_settings(STOREFRONT_ENABLED=True):
            self.assertEqual(Client().get("/products/kopi/").status_code, 200)
