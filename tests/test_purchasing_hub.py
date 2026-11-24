"""Ringkasan pembelian: the purchasing flow on one page, with what waits at each step.

A late purchase order (expected yesterday), a goods receipt not yet billed (5 × 60.000) and a bill 10 days past due
(250.000) each show up in their step, their list and the tiles. The warehouse hand sees the first three steps and no
payables."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import purchasing, receipts
from apps.accounting.models import Supplier
from apps.accounting.payables import create_bill
from apps.shops.models import StoreStaff

D = Decimal


class PurchasingHubTests(Base):
    url = reverse("merchant-purchasing")

    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="CV Sumber")
        self.po = purchasing.create_po(self.store, supplier=self.supplier, order_date=self.today - timedelta(days=5),
            expected_on=self.today - timedelta(days=1), lines=[{"sku": "KBL", "qty": 20, "unit_cost": D(58_000)}])
        self.receipt = receipts.create_receipt(self.store, supplier=self.supplier, received_on=self.today,
            number="SJ-77", lines=[{"sku": "LMP", "qty": 5, "unit_cost": D(60_000)}])
        self.bill = create_bill(self.store, supplier=self.supplier, number="FK-9", bill_date=self.today - timedelta(days=40),
            due_date=self.today - timedelta(days=10), category="6300",
            lines=[{"description": "Jasa", "qty": 1, "unit_price": D(250_000)}])

    def test_each_step_counts_what_waits(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        steps = {s["title"]: (s["count"], s["late"]) for s in page.context["steps"]}
        self.assertEqual(steps["Pesanan pembelian"], (1, 1))
        self.assertEqual(steps["Penerimaan barang"], (1, 0))
        self.assertEqual(steps["Tagihan supplier"], (1, 1))
        totals = page.context["totals"]
        self.assertEqual((totals["on_order"], totals["unbilled"], totals["overdue"]),
            (D(1_160_000), D(300_000), D(250_000)))
        for text in (self.po.number, "SJ-77", "FK-9", "Pesanan pembelian baru", "Beli tunai"):
            self.assertContains(page, text)
        self.assertContains(page, reverse("merchant-supplier-pay", args=[self.supplier.pk]))

    def test_the_sidebar_opens_on_it_and_lists_the_flow_in_order(self):
        self.client.force_login(self.owner)
        html = self.client.get(self.url).content.decode()
        order = [html.index(label) for label in ("Ringkasan pembelian", "Usulan pembelian", "Pesanan pembelian",
            "Penerimaan barang", "Tagihan supplier")]
        self.assertEqual(order, sorted(order))

    def test_the_warehouse_hand_sees_no_payables(self):
        hand = User.objects.create_user("gudang-hub")
        StoreStaff.objects.create(store=self.store, user=hand, role="warehouse")
        self.client.force_login(hand)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual([s["title"] for s in page.context["steps"]],
            ["Usulan pembelian", "Pesanan pembelian", "Penerimaan barang"])
        self.assertNotContains(page, "FK-9")
        self.assertNotContains(page, "Utang usaha")
