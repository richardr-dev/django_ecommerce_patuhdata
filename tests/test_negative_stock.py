"""Stok minus. The first test is the worked example from architecture.md, number for number:
100 units at Rp60.000, sell 120, then receive 50 at Rp65.000."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import controls, inventory
from apps.accounting.ledger import COGS, INVENTORY, account_balances
from apps.accounting.models import Supplier
from apps.accounting.receipts import create_receipt
from apps.orders import deficits
from apps.orders.models import InventoryMovement, StockDeficit
from apps.orders.stock import record_opening_balances, stock_audit
from apps.shops.models import StoreStaff
from apps.tax.posting import void_invoice
from apps.tax.sales import StockShortage, create_sales_invoice


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class NegativeStockTests(Base):
    """Base gives Kabel: 100 on the shelf, cost Rp60.000, price Rp100.000."""

    def setUp(self):
        super().setUp()
        self.store.allow_negative_stock = True
        self.store.save(update_fields=["allow_negative_stock"])
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        record_opening_balances(self.store)   # so the movement ledger explains the starting stock
        self.before = balances(self.store)
        # Base puts stock on the shelf without an opening entry, so books and shelf start apart by a
        # fixed amount; stok minus must never change that gap.
        self.gap = inventory.reconciliation(self.store)["difference"]

    def delta(self, code):
        return balances(self.store).get(code, 0) - self.before.get(code, 0)

    def sell(self, qty, actor="owner"):
        return create_sales_invoice(self.store, buyer_name="Budi", on_credit=True,
            lines=[{"product": self.kabel, "qty": qty, "unit_price": Decimal(100_000)}],
            actor=self.owner if actor == "owner" else actor)

    def receive(self, qty, cost):
        return create_receipt(self.store, supplier=self.supplier, received_on=self.today, actor=self.owner,
            lines=[{"sku": "KBL", "qty": Decimal(qty), "unit_cost": Decimal(cost)}])

    def test_the_worked_example(self):
        # 1. Sell 120 with 100 on the shelf.
        self.sell(120)
        self.assertEqual(self.on_hand(self.kabel), 0)                      # the shelf never goes below zero
        self.assertEqual(deficits.summary(self.store), {"units": 20, "items": 1, "value": Decimal(1_200_000)})
        self.assertEqual(self.delta(COGS), Decimal(7_200_000))            # 120 x 60.000, provisional for 20
        self.assertEqual(self.delta(INVENTORY), Decimal(-7_200_000))
        kinds = list(InventoryMovement.objects.filter(order_item__isnull=False).order_by("id")
            .values_list("kind", "quantity"))
        self.assertEqual(kinds, [("reserve", 100), ("deficit", 20), ("commit", 100)])  # all 120 are traceable
        self.assertEqual(controls.negative_stock(self.store, self.today, {}).status, controls.WARN)
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)   # books and shelf still agree

        # 2. Receive 50 at 65.000: the first 20 cover the minus, HPP is corrected by 20 x 5.000.
        self.receive(50, 65_000)
        self.assertEqual(self.on_hand(self.kabel), 30)
        self.assertEqual(deficits.summary(self.store)["units"], 0)
        self.assertEqual(self.delta(COGS), Decimal(7_300_000))            # final cost of goods sold
        self.kabel.refresh_from_db()
        self.assertEqual(self.kabel.cost_price, Decimal("65000.00"))       # new moving average
        shelf_value = self.on_hand(self.kabel) * self.kabel.cost_price
        self.assertEqual(shelf_value, Decimal(1_950_000))                  # 30 x 65.000
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)
        last = list(InventoryMovement.objects.order_by("-id").values_list("kind", "quantity")[:2])
        self.assertEqual(last, [("deficit_cover", 20), ("receipt", 50)])
        self.assertEqual(controls.negative_stock(self.store, self.today, {}).status, controls.OK)
        self.assertEqual(stock_audit(self.store), [])                      # every unit explained

    def test_refused_when_the_store_does_not_allow_it(self):
        self.store.allow_negative_stock = False
        self.store.save(update_fields=["allow_negative_stock"])
        with self.assertRaises(StockShortage):
            self.sell(120)
        self.assertEqual(self.on_hand(self.kabel), 100)

    def test_only_admin_and_manager_may_sell_short(self):
        for role, allowed in [("finance", False), ("ops", False), ("warehouse", False), ("manager", True)]:
            user = User.objects.create_user(role, password="x")
            StoreStaff.objects.create(store=self.store, user=user, role=role)
            if allowed:
                self.sell(101, actor=user)
            else:
                with self.assertRaises(StockShortage) as raised:
                    self.sell(101, actor=user)
                self.assertIn("admin atau manajer", str(raised.exception))
        with self.assertRaises(StockShortage):       # automatic jobs (no actor) never sell short
            create_sales_invoice(self.store, buyer_name="X",
                lines=[{"product": self.kabel, "qty": 500, "unit_price": Decimal(1)}])

    def test_a_product_without_cost_is_refused_not_guessed(self):
        self.kabel.cost_price = None
        self.kabel.save(update_fields=["cost_price"])
        with self.assertRaises(StockShortage) as raised:
            self.sell(120)
        self.assertIn("harga pokok", str(raised.exception))

    def test_a_small_receipt_covers_part_and_the_rest_stays_open(self):
        self.sell(120)
        self.receive(5, 70_000)
        self.assertEqual(self.on_hand(self.kabel), 0)
        self.assertEqual(deficits.summary(self.store)["units"], 15)
        self.assertEqual(self.delta(COGS), Decimal(7_200_000 + 5 * 10_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)
        self.receive(20, 60_000)                                            # same as provisional: no correction
        self.assertEqual(self.on_hand(self.kabel), 5)
        self.assertEqual(self.delta(COGS), Decimal(7_250_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)

    def test_voiding_the_sale_releases_an_open_deficit(self):
        invoice = self.sell(120)
        void_invoice(invoice, actor=self.owner)
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertEqual(deficits.summary(self.store)["units"], 0)
        self.assertEqual(self.delta(COGS), 0)
        self.assertEqual(self.delta(INVENTORY), 0)
        self.receive(10, 60_000)                                            # nothing left to cover
        self.assertEqual(self.on_hand(self.kabel), 110)

    def test_voiding_after_cover_returns_the_units_and_reverses_the_correction(self):
        invoice = self.sell(120)
        self.receive(50, 65_000)
        void_invoice(invoice, actor=self.owner)
        self.assertEqual(self.on_hand(self.kabel), 150)
        self.assertEqual(self.delta(COGS), 0)
        self.assertTrue(StockDeficit.objects.get().released_at)

    def test_page_switch_and_checklist(self):
        self.client.force_login(self.owner)
        self.sell(120)
        page = self.client.get(reverse("merchant-stock-minus"))
        self.assertContains(page, "−20")
        self.assertContains(page, "Diizinkan")
        self.client.post(reverse("merchant-stock-minus"), {"allow": "0"})
        self.store.refresh_from_db()
        self.assertFalse(self.store.allow_negative_stock)
        self.assertContains(self.client.get(reverse("merchant-year-close")), "Tidak ada stok minus")
        finance = User.objects.create_user("fin", password="x")
        StoreStaff.objects.create(store=self.store, user=finance, role="finance")
        self.client.force_login(finance)
        self.client.post(reverse("merchant-stock-minus"), {"allow": "1"})
        self.store.refresh_from_db()
        self.assertFalse(self.store.allow_negative_stock)                   # finance cannot switch it on


class OutletRequestTests(Base):
    """An outlet asking the warehouse for goods is a transfer inside one company: stock moves, the
    books do not."""

    def test_transfer_moves_stock_between_locations_without_a_journal(self):
        from apps.accounting.models import JournalEntry
        from apps.catalog.models import InventoryLevel, Warehouse
        from apps.orders.stock import StockError, transfer_stock
        warehouse = Warehouse.objects.get(store=self.store)
        outlet = Warehouse.objects.create(store=self.store, code="OUT1", name="Outlet Kemang", is_outlet=True)
        entries, cost = JournalEntry.objects.count(), self.kabel.cost_price
        transfer_stock(store=self.store, product=self.kabel, quantity=30, source=warehouse, target=outlet,
            reason="Permintaan Outlet Kemang", actor=self.owner)
        self.assertEqual(InventoryLevel.objects.get(warehouse=warehouse, product=self.kabel).on_hand, 70)
        self.assertEqual(InventoryLevel.objects.get(warehouse=outlet, product=self.kabel).on_hand, 30)
        self.assertEqual(list(InventoryMovement.objects.order_by("id").values_list("kind", "quantity")),
            [("transfer_out", 30), ("transfer_in", 30)])
        self.assertEqual(JournalEntry.objects.count(), entries)          # no accounting entry
        self.kabel.refresh_from_db()
        self.assertEqual(self.kabel.cost_price, cost)                    # average cost unchanged
        with self.assertRaises(StockError):                              # a transfer never goes minus
            transfer_stock(store=self.store, product=self.kabel, quantity=500, source=warehouse, target=outlet)
