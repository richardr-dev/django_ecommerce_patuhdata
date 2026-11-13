"""Stock card, slow-moving stock and the inventory close picture."""
import io
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from openpyxl import load_workbook
from test_sales_docs import Base

from apps.accounting import closing, inventory_reports
from apps.accounting.models import Supplier
from apps.accounting.receipts import create_receipt
from apps.orders.stock import record_opening_balances


class InventoryReportTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)

    def test_the_stock_card_lists_only_movements_that_change_the_count(self):
        self.invoice(kabel=7, lampu=0)
        supplier = Supplier.objects.create(store=self.store, name="S")
        create_receipt(self.store, supplier=supplier, received_on=self.today, lines=[{"sku": "KBL", "qty": Decimal(20), "unit_cost": Decimal(60_000)}])
        card = inventory_reports.stock_card(self.store, self.kabel)
        self.assertEqual([r["balance"] for r in card["rows"]], [100, 93, 113])      # opening 100, sale -7, receipt +20
        self.assertEqual((card["total_in"], card["total_out"], card["closing"]), (120, 7, 113))
        kinds = [r["move"].kind for r in card["rows"]]
        self.assertNotIn("reserve", kinds)                                          # reservations do not change the count
        self.assertEqual(card["rows"][-1]["balance"], self.on_hand(self.kabel))

    def test_the_card_can_start_later_and_exports(self):
        self.invoice(kabel=7, lampu=0)
        card = inventory_reports.stock_card(self.store, self.kabel, start=self.today + timedelta(days=1))
        self.assertEqual((card["opening"], card["rows"]), (93, []))
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-stock-card"), {"sku": "KBL"})
        self.assertContains(page, "Saldo awal")
        wb = load_workbook(io.BytesIO(self.client.get(reverse("merchant-stock-card"), {"sku": "KBL", "format": "xlsx"}).content))
        self.assertEqual(wb.sheetnames, ["Kartu stok"])
        self.assertContains(self.client.get(reverse("merchant-stock-card"), {"sku": "zzz"}), "tidak ditemukan")

    def test_slow_moving_finds_stock_that_has_not_left(self):
        self.invoice(kabel=1, lampu=0)                                              # Kabel sold today, Lampu never
        report = inventory_reports.slow_moving(self.store, days=90, today=self.today)
        self.assertEqual([r["product"].sku for r in report["rows"]], ["LMP"])
        self.assertEqual(report["rows"][0]["value"], Decimal(100 * 30_000))
        later = inventory_reports.slow_moving(self.store, days=90, today=self.today + timedelta(days=200))
        self.assertEqual({r["product"].sku for r in later["rows"]}, {"KBL", "LMP"})
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-slow-moving"), {"days": "30"}), "Nilai tertahan")

    def test_the_close_picture_turns_yellow_for_open_documents(self):
        from apps.accounting.models import StockCount
        from apps.accounting.stock_counts import open_count
        from apps.catalog.models import Warehouse
        picture = inventory_reports.close_picture(self.store, self.today)
        titles = {i["title"]: i["ok"] for i in picture["items"]}
        self.assertTrue(titles["Stok minus (nilai sementara)"])
        open_count(self.store, warehouse=Warehouse.objects.get(store=self.store), count_date=self.today)
        picture = inventory_reports.close_picture(self.store, self.today)
        self.assertFalse(next(i for i in picture["items"] if i["title"] == "Stok opname belum disetujui")["ok"])
        self.assertFalse(picture["ready"])
        StockCount.objects.update(status="cancelled")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-inventory-close")), "Penutupan persediaan")

    def test_the_month_checklist_points_at_the_inventory_close(self):
        steps, _first, _last = closing.month_checklist(self.store, self.today.year, self.today.month, self.today)
        step = next(s for s in steps if s["title"] == "Persediaan siap ditutup")
        self.assertIn("/penutupan-persediaan/", step["url"])
