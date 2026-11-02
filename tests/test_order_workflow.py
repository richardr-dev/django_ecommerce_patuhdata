"""The back-office order workflow: stage tabs, one-click pack, ship with a tracking number,
received and completed, printable labels."""
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store, StoreStaff


class OrderWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko",
            support_phone="0811")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.product,
            on_hand=10, reserved=1)
        self.client = Client()
        self.client.force_login(self.owner)

    def _order(self, status="paid", name="Budi", **fields):
        order = Order.objects.create(store=self.store, customer_name=name, phone="081234567890",
            shipping_address="Jl. Merdeka 1, Bandung", subtotal=Decimal(50000),
            total=Decimal(50000), status=status, idempotency_key=uuid.uuid4().hex, **fields)
        OrderItem.objects.create(order=order, product=self.product, name="Kopi", sku="KOPI",
            unit_price=50000, quantity=1, line_total=50000)
        return order

    def test_pack_moves_a_paid_order_through_every_warehouse_step_in_one_click(self):
        order = self._order()
        self.client.post(f"/merchant/orders/{order.id}/advance/", {"to": "packed"})

        order.refresh_from_db()
        self.assertEqual(order.status, "packed")
        self.assertEqual(list(order.status_events.values_list("status", flat=True)),
            ["confirmed", "allocated", "picking", "packed"])

    def test_ship_saves_the_tracking_number_and_packs_first_if_needed(self):
        order = self._order()
        response = self.client.post(f"/merchant/orders/{order.id}/ship/", {"courier_code": "JNE",
            "tracking_number": "JNE123", "fulfillment_method": "merchant_dropoff",
            "next": "/merchant/orders/?tab=ship"})

        order.refresh_from_db()
        self.assertRedirects(response, "/merchant/orders/?tab=ship", fetch_redirect_response=False)
        self.assertEqual((order.status, order.courier_code, order.tracking_number),
            ("shipped", "jne", "JNE123"))

    def test_ship_without_a_tracking_number_changes_nothing(self):
        order = self._order(status="packed")
        self.client.post(f"/merchant/orders/{order.id}/ship/", {"courier_code": "jne",
            "tracking_number": ""})

        order.refresh_from_db()
        self.assertEqual(order.status, "packed")

    def test_received_then_completed(self):
        order = self._order(status="shipped")
        self.client.post(f"/merchant/orders/{order.id}/advance/", {"to": "delivered"})
        self.client.post(f"/merchant/orders/{order.id}/advance/", {"to": "completed"})

        order.refresh_from_db()
        self.assertEqual(order.status, "completed")

    def test_steps_cannot_go_backwards_or_skip_payment(self):
        done, unpaid = self._order(status="completed"), self._order(status="pending")
        self.client.post(f"/merchant/orders/{done.id}/advance/", {"to": "packed"})
        self.client.post(f"/merchant/orders/{unpaid.id}/advance/", {"to": "packed"})

        done.refresh_from_db()
        unpaid.refresh_from_db()
        self.assertEqual((done.status, unpaid.status), ("completed", "pending"))

    def test_customer_service_cannot_pack_but_warehouse_can(self):
        order = self._order()
        for role, expected in (("customer_service", "paid"), ("warehouse", "packed")):
            staff = User.objects.create_user(role)
            StoreStaff.objects.create(store=self.store, user=staff, role=role)
            client = Client()
            client.force_login(staff)
            client.post(f"/merchant/orders/{order.id}/advance/", {"to": "packed"})
            order.refresh_from_db()
            self.assertEqual(order.status, expected, role)

    def test_tabs_open_where_the_work_is_and_count_each_stage(self):
        self._order(status="packed")
        self._order(status="paid")
        page = self.client.get("/merchant/orders/")

        self.assertEqual(page.context["tab"], "pack")
        counts = {tab["key"]: tab["count"] for tab in page.context["tabs"]}
        self.assertEqual((counts["pack"], counts["ship"], counts["check"]), (1, 1, 0))
        self.assertContains(page, "Kemas pesanan")
        self.assertContains(self.client.get("/merchant/orders/?tab=ship"), "Nomor resi")

    def test_search_by_name_or_short_order_number(self):
        target = self._order(name="Sari")
        self._order(name="Budi")

        by_name = self.client.get("/merchant/orders/", {"tab": "all", "q": "sari"})
        by_id = self.client.get("/merchant/orders/", {"tab": "all", "q": str(target.id)[:8]})
        self.assertEqual([order.id for order in by_name.context["orders"]], [target.id])
        self.assertEqual([order.id for order in by_id.context["orders"]], [target.id])

    def test_labels_print_the_tab_or_one_order_for_this_store_only(self):
        first, second = self._order(name="Sari"), self._order(name="Budi")
        other = Store.objects.create(owner=User.objects.create_user("x"), name="Lain", slug="lain")
        Order.objects.create(store=other, customer_name="Asing", phone="1", shipping_address="x",
            subtotal=1, total=1, status="paid", idempotency_key="other")

        tab = self.client.get("/merchant/orders/labels/?tab=pack")
        single = self.client.get(f"/merchant/orders/labels/?id={first.id}")
        self.assertEqual(len(tab.context["orders"]), 2)
        self.assertNotContains(tab, "Asing")
        self.assertContains(single, "Sari")
        self.assertNotContains(single, second.customer_name)
        self.assertContains(single, "Jl. Merdeka 1, Bandung")

    def test_unsafe_next_falls_back_to_the_orders_page(self):
        order = self._order()
        response = self.client.post(f"/merchant/orders/{order.id}/advance/", {"to": "packed",
            "next": "https://evil.example/"})
        self.assertRedirects(response, "/merchant/orders/", fetch_redirect_response=False)


class BarcodeTests(TestCase):
    def test_code128_patterns_are_well_formed(self):
        from apps.orders.barcodes import PATTERNS

        self.assertEqual(len(PATTERNS), 107)
        self.assertTrue(all(sum(map(int, pattern)) == 11 for pattern in PATTERNS[:106]))
        self.assertEqual(sum(map(int, PATTERNS[106])), 13)
        self.assertEqual(len(set(PATTERNS)), 107)

    def test_values_carry_start_checksum_and_stop(self):
        from apps.orders.barcodes import code128_svg, code128_values

        values = code128_values("JNE123")
        data = [ord(character) - 32 for character in "JNE123"]
        checksum = (104 + sum(i * v for i, v in enumerate(data, 1))) % 103
        self.assertEqual(values, [104, *data, checksum, 106])
        # Three bars per symbol, four in the stop pattern.
        self.assertEqual(code128_svg("JNE123").count("<rect x="), 3 * (len(values) - 1) + 4)
        self.assertEqual(code128_svg("résumé"), "")


class LabelPrintingTests(OrderWorkflowTests.__bases__[0]):
    def setUp(self):
        OrderWorkflowTests.setUp(self)
        self.product.weight_grams = 250
        self.product.save()

    _order = OrderWorkflowTests._order

    def test_ticked_orders_print_on_thermal_labels_with_barcodes(self):
        first = self._order(name="Sari", status="packed", tracking_number="JNE123456",
            courier_code="jne")
        second = self._order(name="Budi")
        self._order(name="Tidak dipilih")

        page = self.client.get("/merchant/orders/labels/", {"id": [first.id, second.id]})
        self.assertEqual(len(page.context["orders"]), 2)
        self.assertContains(page, 'class="paper-100x150"')
        self.assertContains(page, "@page { size: 100mm 150mm")
        self.assertContains(page, 'aria-label="Barcode JNE123456"')
        self.assertContains(page, "0,25 kg")
        self.assertContains(page, "NON-COD")
        self.assertNotContains(page, "Tidak dipilih")

    def test_paper_size_can_be_switched_and_opening_marks_printed(self):
        order = self._order()
        page = self.client.get("/merchant/orders/labels/", {"id": order.id, "format": "a4",
            "print": "1"})
        self.assertContains(page, 'class="paper-a4"')
        self.assertContains(page, "print()")
        order.refresh_from_db()
        self.assertIsNotNone(order.label_printed_at)
        self.assertContains(self.client.get("/merchant/orders/?tab=pack"), "Sudah dicetak")
