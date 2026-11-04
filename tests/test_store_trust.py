"""Trust signals on an independent store: rating from verified buyers, orders delivered,
units sold per product, member since, and the Lakuin identity check."""
import uuid

from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductReview, Warehouse
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store
from apps.shops.trust import sold_label, store_trust


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class StoreTrustTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma", return_policy="Retur 7 hari bila produk rusak.")
        self.product = Product.objects.create(store=self.store, name="Eclair", slug="eclair",
            sku="ECL", price=25000)
        warehouse = Warehouse.objects.create(store=self.store, name="Dapur", code="D")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=50)

    def _sale(self, status, quantity=1, rating=None):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=1, total=1, status=status,
            idempotency_key=uuid.uuid4().hex)
        OrderItem.objects.create(order=order, product=self.product, name="Eclair", sku="ECL",
            unit_price=1, quantity=quantity, line_total=1)
        if rating:
            ProductReview.objects.create(store=self.store, product=self.product, order=order,
                user=User.objects.create_user(uuid.uuid4().hex[:10]), rating=rating, body="ok",
                display_name="A.")
        return order

    def test_store_rating_delivered_orders_and_membership(self):
        self._sale("completed", rating=5)
        self._sale("delivered", rating=4)
        self._sale("cancelled", quantity=9)
        trust = store_trust(self.store)
        self.assertEqual((trust["rating"], trust["reviews"], trust["delivered"]), (4.5, 2, 2))
        self.assertFalse(trust["verified"])

        page = Client().get("/")
        self.assertContains(page, "pesanan terkirim")
        self.assertContains(page, "Bergabung")
        self.assertNotContains(page, "✔ Terverifikasi")
        self.assertNotContains(page, "ulasan</span>")  # ratings live on the product page only

    def test_product_cards_show_units_sold_but_no_ratings(self):
        self._sale("paid", quantity=3, rating=5)
        self._sale("cancelled", quantity=40)
        page = Client().get("/?q=eclair")
        self.assertContains(page, "3 terjual")
        self.assertNotContains(page, "card-rating")
        self.assertNotContains(page, "★ 5")
        detail = Client().get("/products/eclair/")
        self.assertContains(detail, "<b>3</b> Terjual", html=False)
        self.assertContains(detail, "Penilaian Produk")
        self.assertEqual((sold_label(950), sold_label(1200), sold_label(12500)),
            ("950", "1rb+", "12rb+"))

    def test_new_stores_show_no_empty_ratings(self):
        page = Client().get("/")
        self.assertNotContains(page, "ulasan</span>")
        self.assertNotContains(page, "card-trust")

    def test_only_lakuin_staff_can_verify_a_store(self):
        admin_user = User.objects.create_superuser("staff", "s@lakuin.test", "x")
        request = RequestFactory().post("/")
        request.user = admin_user
        request._messages = type("M", (), {"add": lambda *a, **k: None})()
        model_admin = site._registry[Store]
        model_admin.mark_verified(request, Store.objects.filter(id=self.store.id))
        self.store.refresh_from_db()
        self.assertLessEqual(self.store.verified_at, timezone.now())

        cache.clear()
        self.assertContains(Client().get("/"), "✔ Terverifikasi")

    def test_checkout_explains_payment_and_returns(self):
        buyer = Client()
        buyer.post(f"/cart/add/{self.product.id}/")
        page = buyer.get("/checkout/")
        self.assertContains(page, "Belanja aman di Ruuma")
        self.assertContains(page, "Retur 7 hari bila produk rusak.")
