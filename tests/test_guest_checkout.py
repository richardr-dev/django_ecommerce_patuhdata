"""Guest checkout: order without an account, find the order again from any device with its
number and WhatsApp number, and keep it when the shopper later signs up or logs in."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.access import order_code
from apps.orders.models import Order
from apps.shops.models import Store

GUEST = {"customer_name": "Ayu", "phone": "0812-3456-7890", "shipping_address": "Jl. Melati 1"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class GuestCheckoutTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma", loyalty_enabled=True)
        self.product = Product.objects.create(store=self.store, name="Eclair", slug="eclair",
            sku="ECL", price=25000)
        warehouse = Warehouse.objects.create(store=self.store, name="Dapur", code="D")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)
        self.guest = Client()

    def _order(self, client=None, **extra):
        client = client or self.guest
        client.post(f"/cart/add/{self.product.id}/")
        client.post("/checkout/", {**GUEST, "save_address": "on", **extra})
        return Order.objects.latest("created_at")

    def test_a_guest_order_has_no_account_and_opens_in_the_same_browser(self):
        order = self._order()
        self.assertIsNone(order.customer_user)
        self.assertEqual(order.phone, "081234567890")
        page = self.guest.get(f"/orders/{order.id}/")
        self.assertContains(page, f"#{order_code(order)}")
        self.assertContains(page, "Lacak pesanan")
        self.assertContains(self.guest.get("/account/orders/"), order_code(order))

    def test_order_number_and_whatsapp_find_the_order_on_another_device(self):
        order = self._order()
        other = Client()
        self.assertEqual(other.get(f"/orders/{order.id}/").status_code, 404)

        reply = other.post("/orders/track/", {"code": f"#{order_code(order).lower()}",
            "phone": "+62 812 3456 7890"})
        self.assertRedirects(reply, f"/orders/{order.id}/", fetch_redirect_response=False)
        self.assertEqual(other.get(f"/orders/{order.id}/").status_code, 200)

    def test_a_wrong_phone_or_number_reveals_nothing(self):
        order = self._order()
        other = Client()
        for code, phone in ((order_code(order), "081111111111"), ("ABCDEF12", GUEST["phone"]),
                (order_code(order)[:6], GUEST["phone"]), (order_code(order), "bukan nomor")):
            page = other.post("/orders/track/", {"code": code, "phone": phone})
            self.assertContains(page, "Pesanan tidak ditemukan")
        self.assertEqual(other.get(f"/orders/{order.id}/").status_code, 404)

    def test_orders_from_another_store_are_not_found(self):
        order = self._order()
        Store.objects.create(owner=User.objects.create_user("x"), name="Lain", slug="lain")
        other = Client(HTTP_X_STORE_SLUG="lain")
        page = other.post("/orders/track/", {"code": order_code(order), "phone": GUEST["phone"]})
        self.assertContains(page, "Pesanan tidak ditemukan")

    def test_signing_up_after_ordering_keeps_the_order_in_the_account(self):
        order = self._order()
        self.guest.post("/account/register/", {"email": "ayu@example.com", "first_name": "Ayu",
            "password1": "Sangat-Rahasia-2026", "password2": "Sangat-Rahasia-2026"})
        order.refresh_from_db()
        self.assertEqual(order.customer_user, User.objects.get(email="ayu@example.com"))

    def test_logging_in_claims_only_unowned_orders_from_this_browser(self):
        mine = self._order()
        owner = User.objects.create_user("lain")
        Order.objects.filter(id=mine.id).update(customer_user=owner)
        guest_order = self._order()
        ayu = User.objects.create_user("ayu", password="Sangat-Rahasia-2026")
        self.guest.login(username="ayu", password="Sangat-Rahasia-2026")

        guest_order.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(guest_order.customer_user, ayu)
        self.assertEqual(mine.customer_user, owner)  # already someone's: left alone


class CourierLinkTests(TestCase):
    """The seller enters courier + tracking number; the shopper checks it on the courier's site."""

    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.order = Order.objects.create(store=self.store, customer_name="Ayu",
            phone="081234567890", shipping_address="Jakarta", subtotal=1, total=1,
            status="packed", idempotency_key="k1")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def test_known_couriers_and_aliases(self):
        from apps.orders.couriers import courier_name, courier_tracking
        self.assertEqual(courier_name("J&T"), "J&T Express")
        self.assertEqual(courier_name("jne"), "JNE")
        self.order.tracking_number, self.order.courier_code = "NJV123", "ninjaxpress"
        self.assertEqual(courier_tracking(self.order)["url"],
            "https://www.ninjaxpress.co/id-id/tracking?id=NJV123")
        self.order.courier_code = "kurir-lokal"
        self.assertEqual(courier_tracking(self.order)["url"], "")

    def test_shipped_order_links_the_shopper_to_the_couriers_tracking_page(self):
        self.seller.post(f"/merchant/orders/{self.order.id}/ship/", {"courier_code": "jne",
            "tracking_number": "CGK1234567890", "fulfillment_method": "merchant_dropoff"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "shipped")

        page = self.client.get(f"/orders/{self.order.id}/?token={self.order.access_token}")
        self.assertContains(page, "CGK1234567890")
        self.assertContains(page, 'href="https://www.jne.co.id/tracking-package"')
        self.assertContains(page, "Lacak di JNE")
        self.assertNotContains(page, "disinkronkan")

    def test_sellers_whatsapp_message_carries_the_tracking_link(self):
        self.order.status, self.order.courier_code = "shipped", "jnt"
        self.order.tracking_number = "JT0001"
        self.order.save()
        page = self.seller.get("/merchant/orders/?tab=all")
        self.assertContains(page, "J%26T%20Express")
        self.assertContains(page, "jet.co.id")
        self.assertNotContains(page, "Simpan &amp; sinkronkan")
