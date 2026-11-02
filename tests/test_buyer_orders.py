"""Pesanan Saya, marketplace style: status tabs, estimated arrival, tracker, confirm received,
buy again, and the phone navigation bar."""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.buyer import estimated_arrival, tracker_steps
from apps.orders.models import Order, OrderItem, OrderStatusEvent
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class BuyerOrderTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Eclair", slug="eclair",
            sku="ECL", price=25000)
        warehouse = Warehouse.objects.create(store=self.store, name="Dapur", code="D")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=10)
        self.buyer = User.objects.create_user("buyer")
        self.client = Client()
        self.client.force_login(self.buyer)

    def _order(self, status, **fields):
        values = {"customer_user": self.buyer, "shipping_eta": "2–4 hari kerja", **fields}
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(50000), total=Decimal(50000),
            status=status, idempotency_key=uuid.uuid4().hex, **values)
        OrderItem.objects.create(order=order, product=self.product, name="Eclair", sku="ECL",
            unit_price=25000, quantity=2, line_total=50000)
        return order

    def test_tabs_count_orders_by_stage(self):
        self._order("pending")
        self._order("paid")
        self._order("shipped")
        self._order("completed")
        page = self.client.get("/account/orders/")

        counts = {tab["key"]: tab["count"] for tab in page.context["tabs"]}
        self.assertEqual(counts, {"unpaid": 1, "packing": 1, "shipping": 1, "done": 1,
            "cancelled": 0})
        shipping = self.client.get("/account/orders/?tab=shipping")
        self.assertEqual([order.status for order in shipping.context["orders"]], ["shipped"])
        self.assertContains(shipping, "Pesanan diterima")

    def test_guests_see_the_orders_this_browser_placed(self):
        order = self._order("paid", customer_user=None)
        guest = Client()
        session = guest.session
        session["customer_order_ids"] = [str(order.id)]
        session.save()

        page = guest.get("/account/orders/")
        self.assertEqual([o.id for o in page.context["orders"]], [order.id])
        self.assertContains(page, "Pesanan dari perangkat ini")
        self.assertEqual(list(Client().get("/account/orders/").context["orders"]), [])

    def test_estimated_arrival_counts_working_days_from_shipping(self):
        order = self._order("shipped")
        shipped_at = datetime(2026, 11, 6, 3, tzinfo=UTC)  # Friday 10:00 WIB
        OrderStatusEvent.objects.filter(order=order).delete()
        event = OrderStatusEvent.objects.create(order=order, status="shipped")
        OrderStatusEvent.objects.filter(id=event.id).update(created_at=shipped_at)
        order = Order.objects.prefetch_related("status_events").get(id=order.id)

        # 2–4 working days after Friday: Tuesday to Thursday.
        self.assertEqual(estimated_arrival(order), (date(2026, 11, 10), date(2026, 11, 12)))

    def test_before_shipping_the_estimate_adds_packing_time(self):
        order = self._order("paid", shipping_eta="1-2 hari")
        with patch("apps.orders.buyer.timezone.localdate", return_value=date(2026, 11, 2)):
            self.assertEqual(estimated_arrival(order), (date(2026, 11, 4), date(2026, 11, 5)))
        self.assertIsNone(estimated_arrival(self._order("completed")))

    def test_tracker_marks_the_steps_reached(self):
        steps = tracker_steps(self._order("shipped"))
        self.assertEqual([step["done"] for step in steps], [True, True, True, True, False])

    def test_buyer_confirms_received_and_can_buy_again(self):
        order = self._order("shipped")
        self.client.post(f"/orders/{order.id}/received/")
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")

        response = self.client.post(f"/orders/{order.id}/buy-again/")
        self.assertRedirects(response, "/cart/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["cart"], {f"p:{self.product.id}": 2})

    def test_nobody_else_can_confirm_an_order(self):
        order = self._order("shipped")
        stranger = Client()
        stranger.force_login(User.objects.create_user("stranger"))
        self.assertEqual(stranger.post(f"/orders/{order.id}/received/").status_code, 404)

    def test_order_page_shows_the_tracker_and_estimate(self):
        order = self._order("packed")
        page = self.client.get(f"/orders/{order.id}/")
        self.assertContains(page, "ESTIMASI TIBA")
        self.assertContains(page, 'class="order-tracker"')

    def test_navigation_shows_active_orders_and_cart_counts(self):
        self._order("paid")
        self._order("completed")
        page = self.client.get("/")
        self.assertContains(page, 'class="bottom-nav"')
        nav = page.content.decode().split('class="bottom-nav"')[1]
        self.assertIn("Pesanan<b>1</b>", nav)
