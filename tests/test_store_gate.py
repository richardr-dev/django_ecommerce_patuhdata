"""Shoppers see a store only once it can take orders; a lapsed store says it is temporarily
closed and takes no new orders; the seller always sees their own store."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.models import StorePaymentSettings
from apps.orders.models import Order, ShippingMethod
from apps.shops.availability import preview_token, store_state
from apps.shops.models import Store


class StoreGateTests(TestCase):
    def setUp(self):
        gate = self.settings(STOREFRONT_GATE=True, PAYMENT_PROVIDER="demo",
            ALLOW_DEMO_PAYMENTS=False)
        gate.enable()
        self.addCleanup(gate.disable)
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma",
            listed_in_directory=True)
        self.shopper = Client()

    def _finish_setup(self):
        product = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="K",
            price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=product, on_hand=5)
        StorePaymentSettings.objects.create(store=self.store, bank_transfer_enabled=True,
            bank_name="BCA", bank_account_number="123", bank_account_holder="Ruuma")
        ShippingMethod.objects.create(store=self.store, code="reg", name="Reguler", flat_rate=1)
        return product

    def test_an_unfinished_store_shows_coming_soon_but_keeps_orders_and_callbacks(self):
        self.assertEqual(store_state(self.store), "setup")
        page = self.shopper.get("/")
        self.assertEqual(page.status_code, 503)
        self.assertContains(page, "Toko sedang disiapkan", status_code=503)
        self.assertContains(page, 'name="robots" content="noindex"', status_code=503)
        self.assertEqual(self.shopper.get("/orders/track/").status_code, 200)
        callback = self.shopper.post("/integrations/webhooks/doku/")
        self.assertNotContains(callback, "Toko sedang disiapkan", status_code=callback.status_code)

    def test_a_finished_store_is_open(self):
        self._finish_setup()
        self.assertEqual(store_state(self.store), "open")
        page = self.shopper.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "store-state-bar")

    def test_the_seller_previews_the_store_before_it_opens(self):
        preview = self.shopper.get(f"/?preview={preview_token(self.store)}")
        self.assertRedirects(preview, "/", fetch_redirect_response=False)
        page = self.shopper.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Mode pratinjau")
        self.assertContains(page, "aktifkan pembayaran")
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain")
        forged = Client().get(f"/?preview={preview_token(other)}")  # another store's token
        self.assertEqual(forged.status_code, 503)

    def test_a_lapsed_store_is_closed_to_new_orders_without_saying_why(self):
        product = self._finish_setup()
        Subscription.objects.create(store=self.store, plan=Plan.objects.get(code="pro"),
            current_period_end=timezone.now() - timedelta(days=10))
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=1, total=1, idempotency_key="k")
        self.assertEqual(store_state(self.store), "closed")

        home = self.shopper.get("/")
        self.assertContains(home, "sedang tutup sementara")
        self.assertNotContains(home, "Langganan berakhir")  # shoppers are not told why
        self.assertNotContains(home, "masa aktif")
        self.assertNotContains(home, "data-quick-add>")
        detail = self.shopper.get("/products/kopi/")
        self.assertContains(detail, "belum bisa dipesan")
        self.assertNotContains(detail, "Beli Sekarang")

        self.assertRedirects(self.shopper.post(f"/cart/add/{product.id}/"), "/",
            fetch_redirect_response=False)
        self.assertEqual(self.shopper.session.get("cart", {}), {})
        self.assertRedirects(self.shopper.get("/checkout/"), "/", fetch_redirect_response=False)
        page = self.shopper.get(f"/orders/{order.id}/?token={order.access_token}")
        self.assertEqual(page.status_code, 200)  # existing orders stay reachable

        seller = Client()
        seller.get(f"/?preview={preview_token(self.store)}")
        self.assertContains(seller.get("/"), "Langganan berakhir")

    def test_the_dashboard_says_whether_shoppers_see_the_store(self):
        seller = Client()
        seller.force_login(self.owner)
        page = seller.get("/merchant/")
        self.assertContains(page, "Toko belum tampil untuk pembeli")
        self.assertContains(page, "Wajib")
        self.assertContains(page, "?preview=")
        self._finish_setup()
        self.assertContains(seller.get("/merchant/"), "Toko sudah tampil untuk pembeli")

    def test_the_directory_lists_open_stores_only(self):
        self._finish_setup()
        unfinished = Store.objects.create(owner=User.objects.create_user("b"), name="Belum",
            slug="belum")
        Product.objects.create(store=unfinished, name="X", slug="x", sku="X", price=1)
        page = Client(HTTP_HOST="testserver").get("/stores/")
        self.assertContains(page, "Ruuma")
        self.assertNotContains(page, "Belum")
