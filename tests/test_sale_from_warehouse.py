"""Selling from one chosen location. An outlet (or a register) sells what is on its own shelf: what
another branch holds does not count, and stok minus, when allowed, is recorded at that location."""
from decimal import Decimal

from test_sales_docs import Base

from apps.catalog.models import InventoryLevel, Warehouse
from apps.orders.models import StockDeficit
from apps.orders.stock import StockError, stock_out, transfer_stock
from apps.tax.sales import InvoiceError, StockShortage, create_sales_invoice


class SaleFromWarehouseTests(Base):
    """Base gives Kabel and Lampu: 100 each in Gudang. The outlet starts with 30 Kabel and no Lampu."""

    def setUp(self):
        super().setUp()
        self.gudang = Warehouse.objects.get(store=self.store)
        self.outlet = Warehouse.objects.create(store=self.store, code="OUT1", name="Outlet Kemang",
            is_outlet=True, priority=200)
        transfer_stock(store=self.store, product=self.kabel, quantity=30, source=self.gudang,
            target=self.outlet, actor=self.owner)

    def level(self, warehouse, product):
        return InventoryLevel.objects.filter(warehouse=warehouse, product=product).first()

    def sell(self, product, qty, warehouse, **kwargs):
        return create_sales_invoice(self.store, buyer_name="Walk-in", actor=self.owner, warehouse=warehouse,
            lines=[{"product": product, "qty": qty, "unit_price": Decimal(100_000)}], **kwargs)

    def test_a_sale_at_the_outlet_takes_only_the_outlets_shelf(self):
        self.sell(self.kabel, 10, self.outlet)
        self.assertEqual(self.level(self.outlet, self.kabel).on_hand, 20)
        self.assertEqual(self.level(self.gudang, self.kabel).on_hand, 70)     # untouched

    def test_the_outlet_cannot_sell_what_only_the_warehouse_holds(self):
        with self.assertRaises(StockShortage) as caught:                       # 30 at the outlet, 70 elsewhere
            self.sell(self.kabel, 40, self.outlet)
        self.assertIn("Outlet Kemang", str(caught.exception))
        self.assertEqual(self.level(self.gudang, self.kabel).on_hand, 70)
        with self.assertRaises(StockShortage):                                 # tracked product, no row here
            self.sell(self.lampu, 1, self.outlet)
        self.assertEqual(self.level(self.gudang, self.lampu).on_hand, 100)

    def test_without_a_location_the_sale_draws_in_priority_order_as_before(self):
        self.sell(self.kabel, 80, None)                                        # 70 from Gudang, 10 from the outlet
        self.assertEqual(self.level(self.gudang, self.kabel).on_hand, 0)
        self.assertEqual(self.level(self.outlet, self.kabel).on_hand, 20)

    def test_stok_minus_at_the_outlet_is_recorded_at_the_outlet(self):
        self.store.allow_negative_stock = True
        self.store.save(update_fields=["allow_negative_stock"])
        self.sell(self.kabel, 35, self.outlet)                                 # 30 there, 5 short
        self.assertEqual(self.level(self.outlet, self.kabel).on_hand, 0)
        self.assertEqual(self.level(self.gudang, self.kabel).on_hand, 70)     # never borrowed from
        deficit = StockDeficit.objects.get(store=self.store)
        self.assertEqual((deficit.quantity, deficit.inventory_level.warehouse_id), (5, self.outlet.id))
        self.sell(self.lampu, 2, self.outlet)                                  # no row at the outlet yet
        lampu = self.level(self.outlet, self.lampu)
        self.assertEqual(lampu.on_hand, 0)
        self.assertEqual(StockDeficit.objects.get(product=self.lampu).inventory_level_id, lampu.id)

    def test_another_companys_or_inactive_location_is_refused(self):
        other = Warehouse.objects.create(store=self.store, code="OFF", name="Tutup", is_active=False)
        with self.assertRaises(InvoiceError):
            self.sell(self.kabel, 1, other)
        with self.assertRaises(StockError):
            stock_out(store=self.store, product=self.kabel, quantity=1, kind="adjust", reason="x", warehouse=other)

    def test_stock_out_from_a_location_takes_there_only(self):
        taken = stock_out(store=self.store, product=self.kabel, quantity=5, kind="adjust", reason="rusak",
            warehouse=self.outlet, actor=self.owner)
        self.assertEqual([(level.warehouse_id, qty) for level, qty in taken], [(self.outlet.id, 5)])
        with self.assertRaises(StockError):
            stock_out(store=self.store, product=self.kabel, quantity=26, kind="adjust", reason="rusak",
                warehouse=self.outlet)
