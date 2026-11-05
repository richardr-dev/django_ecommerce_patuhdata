"""Pesanan manual: the seller creates an order for a chat customer, stock is reserved, and the
customer pays through a link (or the order expires and the stock comes back)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import Order
from apps.orders.services import expire_pending_orders
from apps.shops.models import Store, StoreStaff

FORM = {"customer_name": "Bu Sri", "phone": "0812-3456-7890", "fulfilment": "ship",
    "shipping_address": "Apartemen Metro Park Residence Tower B Unit 05", "city": "Jakarta Barat",
    "shipping_name": "JNE REG", "shipping_cost": "15000", "discount": "5000",
    "payment_method": "gateway", "valid_days": "2", "idempotency_key": "manual-1"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, PLATFORM_DOMAIN="")
class ManualOrderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000)
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=5)
        self.kaos = Product.objects.create(store=self.store, name="Kaos", slug="kaos", sku="KAOS",
            price=100000)
        self.kaos_m = ProductVariant.objects.create(product=self.kaos, name="M", sku="KAOS-M",
            stock=3)
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _create(self, items=(("KOPI", "2", ""), ("KAOS-M", "1", "90000")), **extra):
        data = {**FORM, **extra, "sku": [i[0] for i in items], "quantity": [i[1] for i in items],
            "price": [i[2] for i in items]}
        return self.seller.post("/merchant/orders/new/", data)

    def test_seller_creates_an_order_and_stock_is_reserved(self):
        reply = self._create()
        order = Order.objects.get(source="manual")
        self.assertRedirects(reply, f"/merchant/orders/{order.id}/payment-link/",
            fetch_redirect_response=False)
        # 2 × 50.000 + 1 × 90.000 (custom price) − 5.000 + 15.000 ongkir
        self.assertEqual((order.subtotal, order.discount, order.shipping_cost, order.total),
            (190000, 5000, 15000, 200000))
        self.assertEqual((order.created_by, order.phone, order.status), (self.owner, "081234567890", "pending"))
        self.level.refresh_from_db()
        self.kaos_m.refresh_from_db()
        self.assertEqual((self.level.reserved, self.kaos_m.reserved), (2, 1))
        self.assertGreater(order.payment_expires_at, timezone.now() + timedelta(days=1, hours=23))
        page = self.seller.get(f"/merchant/orders/{order.id}/payment-link/")
        self.assertContains(page, f"/orders/{order.id}/?token={order.access_token}")
        self.assertContains(page, "https://wa.me/6281234567890?text=")
        self.assertEqual(self._create().status_code, 302)  # double submit with the same key
        self.assertEqual(Order.objects.filter(source="manual").count(), 1)

    def test_customer_opens_the_link_and_pays(self):
        self._create()
        order = Order.objects.get(source="manual")
        shopper = Client()
        page = shopper.get(f"/orders/{order.id}/?token={order.access_token}")
        self.assertContains(page, "Bayar sekarang")
        reply = shopper.post(f"/orders/{order.id}/pay/", {"token": str(order.access_token)})
        order.refresh_from_db()
        self.assertTrue(order.payment_checkout_url)
        self.assertEqual(reply["Location"], order.payment_checkout_url)
        self.assertContains(shopper.get("/"), "Menunggu pembayaran")  # remembered on the device
        self.assertEqual(Client().post(f"/orders/{order.id}/pay/").status_code, 404)  # no token

    def test_not_enough_stock_or_unknown_sku_is_refused(self):
        self.assertContains(self._create(items=(("KOPI", "9", ""),)), "tidak cukup")
        self.assertContains(self._create(items=(("NOPE", "1", ""),)), "tidak ditemukan")
        self.assertContains(self._create(items=(("KAOS", "1", ""),)), "punya varian")
        self.assertContains(self._create(fulfilment="ship", shipping_address=""), "Isi alamat")
        self.assertFalse(Order.objects.exists())

    def test_unpaid_manual_orders_expire_and_release_stock(self):
        self._create()
        order = Order.objects.get(source="manual")
        Order.objects.filter(pk=order.pk).update(payment_expires_at=timezone.now() - timedelta(minutes=1))
        expire_pending_orders()
        order.refresh_from_db()
        self.level.refresh_from_db()
        self.assertEqual((order.status, self.level.reserved), ("cancelled", 0))

    def test_staff_without_order_rights_cannot_create(self):
        clerk = User.objects.create_user("gudang")
        StoreStaff.objects.create(store=self.store, user=clerk, role="warehouse")
        client = Client()
        client.force_login(clerk)
        self.assertEqual(client.get("/merchant/orders/new/").status_code, 404)
        self.assertContains(self.seller.get("/merchant/orders/"), "+ Buat pesanan")
