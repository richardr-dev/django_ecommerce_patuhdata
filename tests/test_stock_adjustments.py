"""Penyesuaian stok: stock changed for a stated reason moves the shelf and Persediaan together.

Worked example (Kabel 100 at Rp60.000, Lampu 100 at Rp30.000; Saldo awal books Persediaan Rp9.000.000):
- 3 Kabel damaged        → Persediaan rusak 6965 Rp180.000 / Persediaan
- 2 Lampu taken by owner → Prive 3200 Rp60.000 / Persediaan
- 5 Kabel found at 66.000 → Persediaan Rp330.000 / Selisih persediaan 6960; Kabel's average becomes
  (97 × 60.000 + 5 × 66.000) / 102 = Rp60.294,12
Persediaan ends at 9.000.000 − 180.000 − 60.000 + 330.000 = Rp9.090.000 and still equals the stock value."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import inventory, opening, stock_adjustments
from apps.accounting.approvals import approve
from apps.accounting.controls import _balances, stock_changes_booked
from apps.accounting.ledger import DAMAGED_STOCK, DRAWINGS, INVENTORY, STOCK_DIFFERENCE
from apps.accounting.models import ApprovalPolicy, ApprovalRequest, StockAdjustment
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.inventory import record_movement
from apps.orders.models import InventoryMovement
from apps.orders.stock import record_opening_balances, stock_audit
from apps.shops.models import Store, StoreStaff

D = Decimal
AdjustmentError = stock_adjustments.AdjustmentError


class Started(Base):
    """The books have begun: Saldo awal booked the shelf."""

    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.warehouse = Warehouse.objects.get(store=self.store)
        stock = inventory.opening_stock(self.store)
        opening.post_opening(self.store, date=self.today, stock=(stock["shelf_value"], stock["mitra_value"]))

    def adjust(self, reason, *lines, **kwargs):
        return stock_adjustments.post_adjustment(self.store, warehouse=self.warehouse, adjusted_on=self.today,
            reason=reason, actor=self.owner, lines=[{"product": product, "quantity": qty, "unit_cost": cost}
                for product, qty, cost in lines], **kwargs)


class WorkedExampleTests(Started):
    def test_the_worked_example(self):
        self.assertEqual(balance(self.store, INVENTORY), D(9_000_000))
        damaged = self.adjust("damaged", (self.kabel, -3, None))
        self.adjust("owner", (self.lampu, -2, None))
        found = self.adjust("found", (self.kabel, 5, D(66_000)))
        self.assertEqual((damaged.number, found.number), (f"PS/{self.today:%Y}/0001", f"PS/{self.today:%Y}/0003"))
        self.assertEqual((self.on_hand(self.kabel), self.on_hand(self.lampu)), (102, 98))
        self.assertEqual((balance(self.store, DAMAGED_STOCK), balance(self.store, DRAWINGS),
            balance(self.store, STOCK_DIFFERENCE)), (D(180_000), D(-60_000), D(-330_000)))  # Prive is equity: debit shows minus
        self.assertEqual(balance(self.store, INVENTORY), D(9_090_000))
        self.kabel.refresh_from_db()
        self.assertEqual(self.kabel.cost_price, D("60294.12"))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], 0)
        self.assertEqual(stock_audit(self.store), [])
        moves = InventoryMovement.objects.filter(source_type="stock_adjustment", source_id=str(damaged.pk))
        self.assertEqual([(m.kind, m.previous_on_hand, m.new_on_hand) for m in moves], [("adjust", 100, 97)])
        self.assertEqual(stock_changes_booked(self.store, self.today, _balances(self.store)).status, "ok")

    def test_void_puts_the_stock_back_and_reverses_the_entry(self):
        damaged = self.adjust("damaged", (self.kabel, -3, None))
        stock_adjustments.void_adjustment(damaged, actor=self.owner)
        damaged.refresh_from_db()
        self.assertEqual((damaged.status, self.on_hand(self.kabel)), (StockAdjustment.VOID, 100))
        self.assertEqual((balance(self.store, DAMAGED_STOCK), balance(self.store, INVENTORY)), (D(0), D(9_000_000)))
        with self.assertRaises(AdjustmentError):
            stock_adjustments.void_adjustment(damaged)

    def test_refused_never_guessed(self):
        nocost = Product.objects.create(store=self.store, sku="NC", name="Tanpa HPP", slug="nc", price=1)
        InventoryLevel.objects.create(warehouse=self.warehouse, product=nocost, on_hand=0)
        with self.assertRaisesMessage(AdjustmentError, "belum punya harga pokok"):
            self.adjust("opening", (nocost, 5, None))
        with self.assertRaisesMessage(AdjustmentError, "hanya untuk mengurangi"):
            self.adjust("damaged", (self.kabel, 2, None))
        with self.assertRaisesMessage(AdjustmentError, "kurang"):
            self.adjust("lost", (self.kabel, -101, None))
        self.assertFalse(StockAdjustment.objects.exists())
        self.assertEqual(self.on_hand(self.kabel), 100)

    def test_above_the_threshold_waits_for_someone_else(self):
        ApprovalPolicy.objects.create(store=self.store, kind="stock_adjustment", threshold=100_000, enabled=True)
        checker = User.objects.create_user("sa-checker")
        StoreStaff.objects.create(store=self.store, user=checker, role="manager")
        request = self.adjust("lost", (self.kabel, -5, None))
        self.assertIsInstance(request, ApprovalRequest)
        self.assertEqual((self.on_hand(self.kabel), request.amount), (100, D(300_000)))
        approve(request, checker)
        adjustment = StockAdjustment.objects.get()
        self.assertEqual((adjustment.created_by, self.on_hand(self.kabel)), (self.owner, 95))
        self.assertEqual(balance(self.store, STOCK_DIFFERENCE), D(300_000))

    def test_a_quantity_typed_without_a_document_is_flagged(self):
        level = InventoryLevel.objects.get(product=self.lampu)
        level.on_hand = 90
        level.save()
        record_movement(store=self.store, inventory_level=level, kind="adjust", quantity=10, reason="ketik",
            previous_on_hand=100, new_on_hand=90, previous_reserved=0, new_reserved=0)
        check = stock_changes_booked(self.store, self.today, _balances(self.store))
        self.assertEqual(check.status, "warn")
        self.assertIn("1 perubahan stok", check.summary)


class KelolaStokTests(Started):
    def test_kelola_stok_makes_a_penyesuaian_once_the_books_run(self):
        self.client.force_login(self.owner)
        level = InventoryLevel.objects.get(product=self.lampu)
        self.client.post(reverse("merchant-stock"), {"target_type": "level", "target_id": level.pk, "stock": "95",
            "safety_stock": "0", "reason_code": "lost", "reason": "Pecah saat bongkar"})
        adjustment = StockAdjustment.objects.get()
        self.assertEqual((adjustment.reason, adjustment.value, adjustment.notes), ("lost", D(-150_000),
            "Pecah saat bongkar"))
        self.assertEqual((self.on_hand(self.lampu), balance(self.store, INVENTORY)), (95, D(8_850_000)))

    def test_product_form_stock_change_is_a_correction(self):
        from apps.orders.forms import ProductForm
        form = ProductForm.__new__(ProductForm)
        form.fields, form.cleaned_data, form.stock_warehouse = {"stock": None}, {"stock": 104}, self.warehouse
        form.save_stock(self.kabel, self.owner)
        adjustment = StockAdjustment.objects.get()
        self.assertEqual((adjustment.reason, adjustment.value), ("correction", D(240_000)))

    def test_pages(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("merchant-stock-adjustment-new"), {"idem": "k1", "reason": "sample",
            "adjusted_on": self.today.isoformat(), "sku[]": ["KBL"], "qty[]": ["2"], "sign[]": ["-"], "cost[]": [""],
            "notes": "Endorse"})
        adjustment = StockAdjustment.objects.get()
        self.assertRedirects(response, reverse("merchant-stock-adjustment", args=[adjustment.pk]))
        self.assertEqual(adjustment.value, D(-120_000))
        again = self.client.post(reverse("merchant-stock-adjustment-new"), {"idem": "k1", "reason": "sample",
            "sku[]": ["KBL"], "qty[]": ["2"]})
        self.assertEqual((again.status_code, StockAdjustment.objects.count()), (302, 1))  # same key: no second one
        detail = self.client.get(reverse("merchant-stock-adjustment", args=[adjustment.pk]))
        self.assertContains(detail, "Sampel &amp; promosi")
        self.assertContains(detail, "Rp120.000")
        self.assertContains(self.client.get(reverse("merchant-stock-adjustments")), adjustment.number)
        self.assertContains(self.client.get(reverse("merchant-stock-adjustment-new")), "Barang rusak / kedaluwarsa")


class SetupTests(Base):
    def test_before_the_books_start_stock_is_set_without_a_journal_and_saldo_awal_books_it(self):
        self.client.force_login(self.owner)
        level = InventoryLevel.objects.get(product=self.lampu)
        self.client.post(reverse("merchant-stock"), {"target_type": "level", "target_id": level.pk, "stock": "120",
            "safety_stock": "0"})
        self.assertEqual((self.on_hand(self.lampu), StockAdjustment.objects.count()), (120, 0))
        self.assertTrue(InventoryMovement.objects.filter(source_type="setup").exists())
        stock = inventory.opening_stock(self.store)
        self.assertEqual(stock["shelf_value"], D(6_000_000 + 3_600_000))

    def test_saldo_awal_does_not_book_twice_what_the_books_already_hold(self):
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain-sa")
        self.assertFalse(stock_adjustments.books_started(other))
        record_opening_balances(self.store)
        warehouse = Warehouse.objects.get(store=self.store)
        stock_adjustments.post_adjustment(self.store, warehouse=warehouse, adjusted_on=self.today, reason="opening",
            lines=[{"product": self.kabel, "quantity": 10}])  # books Rp600.000 to Modal pemilik
        stock = inventory.opening_stock(self.store)
        self.assertEqual((stock["booked"], stock["shelf_value"]), (D(600_000), D(9_000_000)))


class MutationTests(Started):
    """Mutasi persediaan: units from the movements, rupiah from Persediaan, both ending where the books end."""

    def test_units_and_value_for_the_period(self):
        from apps.accounting.inventory_reports import mutation
        damaged = self.adjust("damaged", (self.kabel, -3, None))
        invoice = self.invoice(kabel=2)
        report = mutation(self.store, self.today, self.today)
        kabel = next(r for r in report["rows"] if r["sku"] == "KBL")
        self.assertEqual((kabel["opening"], kabel["adjustments"], kabel["sales"], kabel["closing"]), (0, 97, -2, 95))
        self.assertEqual(kabel["closing"], self.on_hand(self.kabel))
        value = report["value"]
        groups = {g["kind"]: (g["debit"], g["credit"]) for g in value["rows"]}
        self.assertEqual(groups["opening"], (D(9_000_000), D(0)))
        self.assertEqual(groups["stock_adjustment"], (D(0), D(180_000)))
        self.assertEqual(groups["invoice"], (D(0), D(120_000)))
        self.assertEqual(value["closing"], balance(self.store, INVENTORY))
        self.assertEqual(value["closing"], D(8_700_000))
        # A void lands in the same group as the document it cancels.
        stock_adjustments.void_adjustment(damaged)
        groups = {g["kind"]: (g["debit"], g["credit"]) for g in mutation(self.store, self.today, self.today)["value"]["rows"]}
        self.assertEqual(groups["stock_adjustment"], (D(180_000), D(180_000)))

        self.client.force_login(self.owner)
        url = reverse("merchant-inventory-mutation") + f"?start={self.today}&end={self.today}"
        page = self.client.get(url)
        self.assertContains(page, "Mutasi persediaan")
        self.assertContains(page, "Penyesuaian stok")
        sheet = self.client.get(url + "&format=xlsx")
        self.assertIn("spreadsheetml", sheet["Content-Type"])
        card = self.client.get(reverse("merchant-stock-card") + "?sku=KBL")
        self.assertContains(card, reverse("merchant-stock-adjustment", args=[damaged.pk]))
        self.assertContains(card, reverse("merchant-invoice", args=[invoice.pk]))
        self.assertEqual(self.client.get(reverse("merchant-report-xlsx", args=["mutasi-persediaan"])).status_code, 200)
