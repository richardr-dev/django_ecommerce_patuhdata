"""Stok opname document: count, a second person approves, stock and books move together."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import inventory, stock_counts
from apps.accounting.ledger import INVENTORY, STOCK_DIFFERENCE, account_balances
from apps.accounting.models import StockCount
from apps.catalog.models import Warehouse
from apps.orders.models import InventoryMovement
from apps.orders.stock import record_opening_balances, stock_audit
from apps.shops.models import StoreStaff


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class StockCountTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.warehouse = Warehouse.objects.get(store=self.store)
        self.gap = inventory.reconciliation(self.store)["difference"]
        self.before = balances(self.store)
        self.count = stock_counts.open_count(self.store, warehouse=self.warehouse, count_date=self.today, actor=self.owner)

    def line(self, sku):
        return self.count.lines.get(product__sku=sku)

    def enter(self, kabel, lampu):
        stock_counts.enter_counts(self.count, {self.line("KBL").pk: kabel, self.line("LMP").pk: lampu})

    def test_opening_freezes_the_system_quantity_and_numbers_the_document(self):
        self.assertEqual(self.count.number, f"SO/{self.today:%Y}/0001")
        self.assertEqual(self.line("KBL").system_qty, 100)
        with self.assertRaises(stock_counts.CountError):                      # one open count per warehouse
            stock_counts.open_count(self.store, warehouse=self.warehouse, count_date=self.today)

    def test_approval_moves_stock_and_books_the_value_of_the_difference(self):
        self.enter(kabel=95, lampu=103)                                       # 5 Kabel missing (60.000), 3 Lampu extra (30.000)
        manager = User.objects.create_user("mgr", password="x")
        StoreStaff.objects.create(store=self.store, user=manager, role="manager")
        stock_counts.approve(self.count, actor=manager)
        self.assertEqual(self.on_hand(self.kabel), 95)
        self.assertEqual(self.on_hand(self.lampu), 103)
        after = balances(self.store)
        self.assertEqual(after[STOCK_DIFFERENCE] - self.before.get(STOCK_DIFFERENCE, 0), Decimal(5 * 60_000 - 3 * 30_000))   # net loss: an expense
        self.assertEqual(after[INVENTORY] - self.before[INVENTORY], Decimal(-5 * 60_000 + 3 * 30_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)    # books and shelf still agree
        self.assertEqual(list(InventoryMovement.objects.filter(kind="opname", source_type="stock_count")
            .order_by("id").values_list("quantity", flat=True)), [5, 3])
        self.assertEqual(stock_audit(self.store), [])
        self.count.refresh_from_db()
        self.assertEqual((self.count.status, self.count.approved_by), (StockCount.APPROVED, manager))
        with self.assertRaises(stock_counts.CountError):                      # approved counts are final
            stock_counts.approve(self.count, actor=manager)

    def test_sales_while_counting_are_not_overwritten(self):
        self.invoice(kabel=10, lampu=0)                                       # shelf 90 while the count froze 100
        self.enter(kabel=100, lampu=100)                                      # the counter still found 100 on the shelf
        manager = User.objects.create_user("mgr", password="x")
        StoreStaff.objects.create(store=self.store, user=manager, role="manager")
        stock_counts.approve(self.count, actor=manager)
        self.assertEqual(self.on_hand(self.kabel), 90)                        # no difference: the sale stands

    def test_the_maker_cannot_approve_but_a_one_person_store_can(self):
        self.enter(kabel=100, lampu=100)
        with self.assertRaises(stock_counts.CountError):
            other = User.objects.create_user("mgr", password="x")
            StoreStaff.objects.create(store=self.store, user=other, role="manager")
            stock_counts.approve(self.count, actor=self.owner)                # the owner opened it, a manager exists
        StoreStaff.objects.filter(user__username="mgr").delete()
        self.assertEqual(stock_counts.approve(self.count, actor=self.owner).status, StockCount.APPROVED)

    def test_incomplete_or_unpriced_counts_are_refused(self):
        with self.assertRaises(stock_counts.CountError):                      # nothing counted yet
            stock_counts.approve(self.count, actor=self.owner)
        self.enter(kabel=99, lampu=100)
        self.lampu.cost_price = None
        self.lampu.save(update_fields=["cost_price"])
        self.line("LMP").counted_qty = 99
        self.line("LMP").save()
        stock_counts.enter_counts(self.count, {self.line("LMP").pk: 99})
        with self.assertRaises(stock_counts.CountError) as raised:
            stock_counts.approve(self.count, actor=self.owner)
        self.assertIn("harga pokok", str(raised.exception))
        self.assertEqual(self.on_hand(self.kabel), 100)                       # nothing moved

    def test_cancel_and_negative_counts(self):
        with self.assertRaises(stock_counts.CountError):
            stock_counts.enter_counts(self.count, {self.line("KBL").pk: -1})
        stock_counts.cancel(self.count)
        self.assertEqual(self.on_hand(self.kabel), 100)
        stock_counts.open_count(self.store, warehouse=self.warehouse, count_date=self.today)   # a new one can start

    def test_pages(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-stock-counts")), self.count.number)
        page = self.client.get(reverse("merchant-stock-count", args=[self.count.pk]))
        self.assertContains(page, "Hasil hitung")
        self.client.post(reverse("merchant-stock-count", args=[self.count.pk]),
            {"action": "save", f"counted_{self.line('KBL').pk}": "97", f"counted_{self.line('LMP').pk}": "100"})
        self.assertEqual(self.line("KBL").counted_qty, 97)
        self.client.post(reverse("merchant-stock-count", args=[self.count.pk]), {"action": "approve",
            f"counted_{self.line('KBL').pk}": "97", f"counted_{self.line('LMP').pk}": "100"})
        self.count.refresh_from_db()
        self.assertEqual(self.count.status, StockCount.APPROVED)
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)
