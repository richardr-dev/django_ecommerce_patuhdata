"""Lot & kedaluwarsa.

Worked example: Susu tracks lots. Lot A (30 units, expires in 60 days) and lot B (20 units, expires in 10 days)
arrive. A sale of 25 draws B first (it expires sooner): B 20 → 0, then A 30 → 25. A credit note returning 3 puts
them back in A, the lot drawn last. A transfer between warehouses leaves the lots alone. Kabel, switched on with
100 already on the shelf, starts with lot AWAL of 100."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import purchasing
from apps.accounting.controls import _balances, lots_tie
from apps.accounting.models import Supplier
from apps.accounting.receipts import create_receipt
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders import lots
from apps.orders.models import StockLot
from apps.orders.stock import record_opening_balances, transfer_stock
from apps.tax.credit_notes import create_credit_note
from apps.tax.sales import create_sales_invoice

D = Decimal


class LotTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.gudang = Warehouse.objects.get(store=self.store)
        self.susu = Product.objects.create(store=self.store, sku="SUSU", name="Susu UHT", slug="susu", price=20_000,
            cost_price=12_000)
        InventoryLevel.objects.create(warehouse=self.gudang, product=self.susu, on_hand=0)
        lots.start_tracking(self.susu)
        self.supplier = Supplier.objects.create(store=self.store, name="PT Susu")
        self.receipt = create_receipt(self.store, supplier=self.supplier, received_on=self.today, number="SJ-77",
            lines=[{"sku": "SUSU", "qty": 30, "unit_cost": D(12_000), "lot": "A", "expiry": self.today + timedelta(days=60)},
                {"sku": "SUSU", "qty": 20, "unit_cost": D(12_000), "lot": "B", "expiry": self.today + timedelta(days=10)}])

    def lot(self, code):
        return StockLot.objects.get(product=self.susu, code=code)

    def held(self):
        return {lot.code: lot.quantity for lot in StockLot.objects.filter(product=self.susu)}

    def status(self):
        return lots_tie(self.store, self.today, _balances(self.store)).status

    def test_the_worked_example(self):
        self.assertEqual(self.held(), {"A": 30, "B": 20})
        self.assertEqual(self.receipt.lines.get(lot_code="B").expiry_date, self.today + timedelta(days=10))
        invoice = create_sales_invoice(self.store, lines=[{"product": self.susu, "qty": 25, "unit_price": D(20_000)}],
            buyer_name="Budi", customer=self.customer, on_credit=True)
        self.assertEqual(self.held(), {"A": 25, "B": 0})
        trail = lots.trace(self.lot("B"))
        self.assertEqual([row["move"].quantity for row in trail], [20, -20])
        self.assertEqual(trail[1]["url"], reverse("merchant-invoice", args=[invoice.pk]))   # the recall answer
        item = invoice.items.get()
        create_credit_note(invoice, lines=[{"item": item, "description": item.description, "quantity": 3,
            "unit_price": item.unit_price, "restock": True}], issued_on=self.today)
        self.assertEqual(self.held(), {"A": 28, "B": 0})
        toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko", priority=200)
        transfer_stock(store=self.store, product=self.susu, quantity=5, source=self.gudang, target=toko)
        self.assertEqual(self.held(), {"A": 28, "B": 0})
        self.assertEqual(self.status(), "ok")

    def test_switching_on_with_stock_and_expiry_warnings(self):
        lots.start_tracking(self.kabel)
        self.assertEqual(StockLot.objects.get(product=self.kabel).code, "AWAL")
        self.assertEqual(StockLot.objects.get(product=self.kabel).quantity, 100)
        self.assertEqual(self.status(), "ok")
        due = lots.expiring(self.store)
        self.assertEqual([lot.code for lot in due["soon"]], ["B"])
        create_receipt(self.store, supplier=self.supplier, received_on=self.today, lines=[{"sku": "SUSU", "qty": 4,
            "unit_cost": D(12_000), "lot": "C", "expiry": self.today - timedelta(days=1)}])
        self.assertEqual([lot.code for lot in lots.expiring(self.store)["expired"]], ["C"])
        self.assertEqual(self.status(), "warn")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-dashboard")), "lot barang sudah kedaluwarsa")

    def test_a_receipt_without_a_lot_code_gets_one_and_untracked_products_get_none(self):
        create_receipt(self.store, supplier=self.supplier, received_on=self.today, number="SJ 9", lines=[
            {"sku": "SUSU", "qty": 2, "unit_cost": D(12_000)}, {"sku": "KBL", "qty": 2, "unit_cost": D(60_000)}])
        code = f"SJ9-{self.today:%y%m%d}-1"
        self.assertEqual(self.lot(code).quantity, 2)
        self.assertFalse(StockLot.objects.filter(product=self.kabel).exists())

    def test_a_purchase_order_receives_into_a_lot(self):
        po = purchasing.create_po(self.store, supplier=self.supplier, order_date=self.today,
            lines=[{"sku": "SUSU", "qty": 12, "unit_cost": D(12_000)}])
        line = po.lines.get()
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-purchase-order", args=[po.pk]), {"action": "receive", "idem": "p1",
            f"qty_{line.pk}": "12", f"lot_{line.pk}": "D-2027", f"exp_{line.pk}": "2027-01-31",
            "received_on": self.today.isoformat()})
        lot = self.lot("D-2027")
        self.assertEqual((lot.quantity, lot.expiry_date.isoformat()), (12, "2027-01-31"))

    def test_the_pages(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-lots"))
        self.assertContains(page, "Susu UHT")
        self.assertEqual([lot.code for lot in page.context["rows"]], ["B", "A"])     # soonest first
        self.assertEqual(len(self.client.get(reverse("merchant-lots") + "?status=soon").context["rows"]), 1)
        self.client.post(reverse("merchant-lots"), {"action": "track", "product": str(self.lampu.pk)})
        self.lampu.refresh_from_db()
        self.assertTrue(self.lampu.tracks_lots)
        detail = self.client.get(reverse("merchant-lot", args=[self.lot("A").pk]))
        self.assertContains(detail, "Ke mana lot ini pergi")
        self.client.post(reverse("merchant-lot", args=[self.lot("A").pk]), {"expiry_date": "2027-06-30"})
        self.assertEqual(self.lot("A").expiry_date.isoformat(), "2027-06-30")


class ReceiptFormLotTests(LotTests):
    def test_the_receipt_form_names_the_lot(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-goods-receipt-new"), {"supplier_id": str(self.supplier.pk), "idem": "g1",
            "received_on": self.today.isoformat(), "sku[]": ["SUSU", "KBL"], "qty[]": ["6", "1"],
            "unit_cost[]": ["12000", "60000"], "lot[]": ["E-1", "ignored"], "expiry[]": ["2027-03-01", ""]})
        self.assertEqual((self.lot("E-1").quantity, self.lot("E-1").expiry_date.isoformat()), (6, "2027-03-01"))
        self.assertFalse(StockLot.objects.filter(product=self.kabel).exists())
        receipt = self.supplier.goods_receipts.order_by("-created_at").first()
        self.assertContains(self.client.get(reverse("merchant-goods-receipt", args=[receipt.pk])), "Lot E-1")


class ExpiredLotTests(LotTests):
    def test_an_expired_lot_is_drawn_only_when_nothing_else_is_left(self):
        create_receipt(self.store, supplier=self.supplier, received_on=self.today, lines=[{"sku": "SUSU", "qty": 4,
            "unit_cost": D(12_000), "lot": "OLD", "expiry": self.today - timedelta(days=2)}])
        create_sales_invoice(self.store, lines=[{"product": self.susu, "qty": 25, "unit_price": D(20_000)}],
            buyer_name="Budi", customer=self.customer, on_credit=True)
        self.assertEqual(self.held(), {"A": 25, "B": 0, "OLD": 4})      # B then A; OLD waits to be written off
        create_sales_invoice(self.store, lines=[{"product": self.susu, "qty": 27, "unit_price": D(20_000)}],
            buyer_name="Budi", customer=self.customer, on_credit=True)
        self.assertEqual(self.held(), {"A": 0, "B": 0, "OLD": 2})
