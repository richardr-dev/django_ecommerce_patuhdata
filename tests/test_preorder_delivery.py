"""Pre-order products with daily quotas, delivery dates and time slots with capacity."""
import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import (
    DeliverySettings,
    DeliverySlot,
    InventoryAllocation,
    Order,
    OrderItem,
)
from apps.orders.scheduling import available_dates, slot_options
from apps.orders.services import transition_order
from apps.shops.models import Store

BUYER = {"customer_name": "Ayu", "email": "ayu@example.com", "phone": "081234567890",
    "shipping_address": "Jakarta"}
MONDAY = date(2026, 11, 2)


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class PreorderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.cake = Product.objects.create(store=self.store, name="Kue ulang tahun", slug="kue",
            sku="KUE", price=250000, is_preorder=True, preorder_days=2, preorder_daily_quota=3)
        self.bread = Product.objects.create(store=self.store, name="Roti", slug="roti", sku="ROTI",
            price=20000)
        warehouse = Warehouse.objects.create(store=self.store, name="Dapur", code="D")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.bread, on_hand=5)
        self.buyer = Client()
        self.buyer.force_login(User.objects.create_user("buyer"))

    def _lines(self, product, quantity=1):
        return [{"product": product, "variant": None, "quantity": quantity}]

    def _book(self, day, quantity=1, status="paid", slot=None):
        order = Order.objects.create(store=self.store, customer_name="X", phone="1",
            shipping_address="x", subtotal=1, total=1, status=status, delivery_date=day,
            delivery_slot=slot, idempotency_key=uuid.uuid4().hex)
        OrderItem.objects.create(order=order, product=self.cake, name="Kue", sku="KUE",
            unit_price=1, quantity=quantity, line_total=1, is_preorder=True)
        return order

    def _checkout(self, product, **extra):
        self.buyer.post(f"/cart/add/{product.id}/")
        return self.buyer.post("/checkout/", {**BUYER, **extra})

    def test_dates_start_after_production_time_and_skip_closed_days(self):
        DeliverySettings.objects.create(store=self.store, closed_weekdays="6", max_days=8)
        days = available_dates(self.store, self._lines(self.cake), today=MONDAY)

        self.assertEqual(days[0], MONDAY + timedelta(days=2))  # Wednesday
        self.assertNotIn(date(2026, 11, 8), days)  # Sunday is closed
        self.assertEqual(days[-1], MONDAY + timedelta(days=8))

    def test_a_date_whose_quota_is_used_up_is_not_offered(self):
        wednesday = MONDAY + timedelta(days=2)
        self._book(wednesday, quantity=2)
        self._book(wednesday, quantity=5, status="cancelled")  # released, does not count

        self.assertIn(wednesday, available_dates(self.store, self._lines(self.cake), today=MONDAY))
        self.assertNotIn(wednesday, available_dates(self.store, self._lines(self.cake, 2),
            today=MONDAY))

    def test_preorder_sells_without_stock_and_keeps_the_chosen_date(self):
        day = available_dates(self.store, self._lines(self.cake))[0]
        self._checkout(self.cake, delivery_date=day.isoformat())

        order = Order.objects.get()
        item = order.items.get()
        self.assertEqual(order.delivery_date, day)
        self.assertTrue(item.is_preorder)
        self.assertFalse(InventoryAllocation.objects.filter(order_item=item).exists())
        self.level.refresh_from_db()
        self.assertEqual(self.level.reserved, 0)

    def test_checkout_rejects_a_missing_or_unavailable_date(self):
        page = self._checkout(self.cake)
        self.assertContains(page, "Pilih tanggal pengiriman")
        page = self.buyer.post("/checkout/", {**BUYER,
            "delivery_date": timezone.localdate().isoformat()})  # before the production time
        self.assertContains(page, "tidak tersedia")
        self.assertFalse(Order.objects.exists())

    def test_normal_products_need_no_date_unless_the_store_schedules_deliveries(self):
        self._checkout(self.bread)
        self.assertIsNone(Order.objects.get().delivery_date)

        DeliverySettings.objects.create(store=self.store, enabled=True, min_days=1)
        self.buyer.post(f"/cart/add/{self.bread.id}/")
        page = self.buyer.post("/checkout/", BUYER)
        self.assertContains(page, "Pilih tanggal pengiriman")

    def test_full_slots_are_refused_and_reported(self):
        DeliverySettings.objects.create(store=self.store, enabled=True, min_days=0)
        slot = DeliverySlot.objects.create(store=self.store, start_time=time(9),
            end_time=time(12), capacity=1)
        day = timezone.localdate() + timedelta(days=3)
        self._book(day, slot=slot)

        self.assertTrue(slot_options(self.store, day)[0]["full"])
        reply = self.buyer.get(f"/checkout/delivery-slots/?date={day.isoformat()}")
        self.assertEqual(reply.json()["slots"][0]["full"], True)
        page = self._checkout(self.bread, delivery_date=day.isoformat(),
            delivery_slot=str(slot.id))
        self.assertContains(page, "sudah penuh")

    def test_packing_or_cancelling_a_preorder_variant_leaves_stock_alone(self):
        variant = ProductVariant.objects.create(product=self.cake, name="20 cm", sku="KUE-20",
            stock=2, reserved=1)
        order = self._book(timezone.localdate() + timedelta(days=3))
        order.items.update(variant=variant)

        transition_order(order, "confirmed")
        transition_order(order, "allocated")
        transition_order(order, "picking")
        transition_order(order, "packed")
        variant.refresh_from_db()
        self.assertEqual((variant.stock, variant.reserved), (2, 1))

        other = self._book(timezone.localdate() + timedelta(days=4), status="pending")
        other.items.update(variant=variant)
        transition_order(other, "cancelled")
        variant.refresh_from_db()
        self.assertEqual(variant.reserved, 1)

    def test_quick_add_works_for_preorder_products_without_stock(self):
        reply = self.buyer.post(f"/cart/add/{self.cake.id}/", HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(reply.json()["count"], 1)
        self.assertContains(self.buyer.get("/"), "Pre-order")

    @patch("apps.orders.scheduling.timezone.localdate", return_value=MONDAY)
    def test_checkout_page_offers_the_dates_and_slots(self, _today):
        DeliverySlot.objects.create(store=self.store, start_time=time(9), end_time=time(12))
        self.buyer.post(f"/cart/add/{self.cake.id}/")
        page = self.buyer.get("/checkout/")
        self.assertContains(page, 'value="2026-11-04"')
        self.assertContains(page, "09.00–12.00")


class DeliveryBackOfficeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.client = Client()
        self.client.force_login(self.owner)

    def test_seller_sets_closed_days_and_adds_slots(self):
        self.client.post("/merchant/delivery/", {"action": "settings", "enabled": "on",
            "min_days": "1", "max_days": "10", "closed_weekdays": ["6"]})
        settings_obj = DeliverySettings.objects.get(store=self.store)
        self.assertEqual((settings_obj.enabled, settings_obj.closed_days), (True, {6}))

        self.client.post("/merchant/delivery/", {"action": "add_slot", "start_time": "09:00",
            "end_time": "12:00", "capacity": "10"})
        slot = DeliverySlot.objects.get(store=self.store)
        self.assertEqual((slot.label, slot.capacity), ("09.00–12.00", 10))

        page = self.client.post("/merchant/delivery/", {"action": "add_slot",
            "start_time": "14:00", "end_time": "13:00"})
        self.assertContains(page, "Jam selesai harus setelah jam mulai")

    def test_recap_totals_products_per_delivery_date(self):
        day = timezone.localdate() + timedelta(days=2)
        product = Product.objects.create(store=self.store, name="Eclair", slug="e", sku="E",
            price=1)
        for quantity in (3, 4):
            order = Order.objects.create(store=self.store, customer_name="Ayu", phone="1",
                shipping_address="x", subtotal=1, total=1, status="paid", delivery_date=day,
                idempotency_key=uuid.uuid4().hex)
            OrderItem.objects.create(order=order, product=product, name="Eclair", sku="E",
                unit_price=1, quantity=quantity, line_total=Decimal(1))

        page = self.client.get(f"/merchant/delivery/recap/?date={day.isoformat()}")
        self.assertEqual(next(iter(page.context["totals"]))["quantity"], 7)
        self.assertContains(page, "2 pesanan dibayar")


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class CartDateAndNoteTests(PreorderTests):
    """The cart picks the order date first and shows each pre-order item's quota for it."""

    def test_cart_shows_dates_and_the_quota_left_for_the_chosen_date(self):
        self.buyer.post(f"/cart/add/{self.cake.id}/")
        dates = available_dates(self.store, self._lines(self.cake))
        self._book(dates[1], quantity=2)

        page = self.buyer.get(f"/cart/?date={dates[1].isoformat()}")
        self.assertContains(page, 'class="cart-dates"')
        self.assertContains(page, "Sisa 1 untuk")
        self.assertEqual(self.buyer.session["delivery_date"], dates[1].isoformat())
        self.assertContains(self.buyer.get("/checkout/"),
            f'value="{dates[1].isoformat()}" required checked')

    def test_going_over_the_dates_quota_blocks_checkout_from_the_cart(self):
        self.buyer.post(f"/cart/add/{self.cake.id}/", {"quantity": 3})
        day = available_dates(self.store, self._lines(self.cake, 3))[0]
        self._book(day, quantity=1)

        page = self.buyer.get(f"/cart/?date={day.isoformat()}")
        self.assertContains(page, "tinggal 2")
        self.assertContains(page, 'class="button disabled"')

    def test_updating_a_preorder_quantity_keeps_it_in_the_cart(self):
        self.buyer.post(f"/cart/add/{self.cake.id}/")
        self.buyer.post(f"/cart/update/p:{self.cake.id}/", {"quantity": 2})
        self.assertEqual(self.buyer.session["cart"], {f"p:{self.cake.id}": 2})

    def test_item_notes_travel_from_product_to_the_order(self):
        self.buyer.post(f"/cart/add/{self.cake.id}/", {"note": "Happy Birthday Ayu"})
        self.assertContains(self.buyer.get("/cart/"), 'value="Happy Birthday Ayu"')
        day = available_dates(self.store, self._lines(self.cake))[0]
        self.buyer.post("/checkout/", {**BUYER, "delivery_date": day.isoformat()})

        item = Order.objects.get().items.get()
        self.assertEqual(item.note, "Happy Birthday Ayu")
        self.assertNotIn("cart_notes", self.buyer.session)
        seller = Client()
        seller.force_login(self.owner)
        self.assertContains(seller.get("/merchant/orders/?tab=all"), "Happy Birthday Ayu")
