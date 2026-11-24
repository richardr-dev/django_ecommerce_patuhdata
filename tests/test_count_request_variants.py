"""Stok opname and stock requests for variants, and a count that only touches the warehouse counted.

Kaos M: 10 in Gudang, 5 in Toko (cost 40.000); Kaos L: 4 in Gudang (cost 45.000). Counting Gudang finds M 8 and
L 5: M −2 and L +1 at Gudang only, valued −80.000 + 45.000 = −35.000 to Selisih persediaan. Toko asks Gudang for
3 M: they leave Gudang's row when sent, 2 arrive in Toko, the missing one is a 40.000 loss in transit."""
from decimal import Decimal

from test_sales_docs import Base, balance

from apps.accounting import stock_counts
from apps.accounting.controls import _balances, lots_tie, stock_movements
from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders import lots, stock_requests
from apps.orders.models import StockLot
from apps.orders.stock import receive_goods, record_opening_balances
from apps.orders.variant_stock import at

D = Decimal


class CountRequestVariantTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.gudang = Warehouse.objects.get(store=self.store)
        self.toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko Depan", priority=200)
        self.kaos = Product.objects.create(store=self.store, sku="KAOS", name="Kaos", slug="kaos", price=80_000)
        self.m = ProductVariant.objects.create(product=self.kaos, name="M", sku="KAOS-M", cost_price=D(40_000))
        self.l = ProductVariant.objects.create(product=self.kaos, name="L", sku="KAOS-L", cost_price=D(45_000))
        for variant, qty, place in ((self.m, 10, self.gudang), (self.m, 5, self.toko), (self.l, 4, self.gudang)):
            receive_goods(store=self.store, product=self.kaos, variant=variant, quantity=qty,
                unit_cost=variant.cost_price, warehouse=place)

    def where(self, variant):
        variant.refresh_from_db()
        return variant.stock, at(variant, self.gudang), at(variant, self.toko)

    def moves_ok(self):
        return stock_movements(self.store, self.today, _balances(self.store)).status

    def test_counting_sizes_in_one_warehouse(self):
        count = stock_counts.open_count(self.store, warehouse=self.gudang, count_date=self.today, actor=self.owner)
        lines = {line.label: line for line in count.lines.select_related("product", "variant")}
        self.assertEqual({k: v.system_qty for k, v in lines.items()},
            {"Kabel": 100, "Lampu": 100, "Kaos / M": 10, "Kaos / L": 4})
        counted = {line.pk: line.system_qty for line in lines.values()}
        counted[lines["Kaos / M"].pk], counted[lines["Kaos / L"].pk] = 8, 5
        stock_counts.enter_counts(count, counted)
        self.assertEqual(stock_counts.summary(count)["value"], D(-35_000))
        before = balance(self.store, "6960")
        stock_counts.approve(count, actor=self.owner)
        self.assertEqual((self.where(self.m), self.where(self.l)), ((13, 8, 5), (5, 5, 0)))
        self.assertEqual(balance(self.store, "6960") - before, D(35_000))
        self.assertEqual(self.moves_ok(), "ok")

    def test_a_shortage_is_taken_from_the_warehouse_counted(self):
        InventoryLevel.objects.create(warehouse=self.toko, product=self.kabel, on_hand=0)
        from apps.orders.stock import stock_in
        stock_in(store=self.store, product=self.kabel, quantity=5, kind="adjust", reason="awal", warehouse=self.toko)
        count = stock_counts.open_count(self.store, warehouse=self.toko, count_date=self.today, actor=self.owner)
        stock_counts.enter_counts(count, {line.pk: (3 if line.product_id == self.kabel.pk and not line.variant_id
            else line.system_qty) for line in count.lines.all()})
        stock_counts.approve(count, actor=self.owner)
        levels = {level.warehouse_id: level.on_hand for level in InventoryLevel.objects.filter(product=self.kabel)}
        self.assertEqual(levels, {self.gudang.pk: 100, self.toko.pk: 3})   # Gudang's 100 untouched

    def test_a_request_moves_a_size_and_books_what_went_missing(self):
        request = stock_requests.create_request(self.store, from_warehouse=self.gudang, to_warehouse=self.toko,
            lines=[{"sku": "KAOS-M", "qty": 3}], actor=self.owner)
        line = request.lines.get()
        self.assertEqual((line.variant, line.label), (self.m, "Kaos / M"))
        with self.assertRaisesMessage(stock_requests.RequestError, "per SKU varian"):
            stock_requests.create_request(self.store, from_warehouse=self.gudang, to_warehouse=self.toko,
                lines=[{"sku": "KAOS", "qty": 1}], actor=self.owner)
        stock_requests.approve(request, actor=self.owner)
        stock_requests.send(request, quantities={line.pk: 3}, actor=self.owner)
        self.assertEqual(self.where(self.m), (12, 7, 5))
        self.assertEqual(stock_requests.in_transit(self.kaos, self.m), 3)
        before = balance(self.store, "6960")
        stock_requests.receive(request, quantities={line.pk: 2}, actor=self.owner)
        self.assertEqual(self.where(self.m), (14, 7, 7))
        self.assertEqual(balance(self.store, "6960") - before, D(40_000))
        self.assertEqual(self.moves_ok(), "ok")

    def test_lots_count_units_on_the_road(self):
        lots.start_tracking(self.lampu)
        toko_level = InventoryLevel.objects.create(warehouse=self.toko, product=self.lampu, on_hand=0)
        request = stock_requests.create_request(self.store, from_warehouse=self.gudang, to_warehouse=self.toko,
            lines=[{"sku": "LMP", "qty": 10}], actor=self.owner)
        line = request.lines.get()
        stock_requests.approve(request, actor=self.owner)
        stock_requests.send(request, quantities={line.pk: 10}, actor=self.owner)
        self.assertEqual(lots_tie(self.store, self.today, _balances(self.store)).status, "ok")   # 90 + 10 on the road
        stock_requests.receive(request, quantities={line.pk: 9}, actor=self.owner)
        toko_level.refresh_from_db()
        self.assertEqual(toko_level.on_hand, 9)
        self.assertEqual(StockLot.objects.get(product=self.lampu).quantity, 99)
        self.assertEqual(lots_tie(self.store, self.today, _balances(self.store)).status, "ok")

    def test_the_pages_name_the_size(self):
        from django.urls import reverse
        count = stock_counts.open_count(self.store, warehouse=self.gudang, count_date=self.today, actor=self.owner)
        request = stock_requests.create_request(self.store, from_warehouse=self.gudang, to_warehouse=self.toko,
            lines=[{"sku": "KAOS-L", "qty": 1}], actor=self.owner)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-stock-count", args=[count.pk])), "Kaos / M")
        self.assertContains(self.client.get(reverse("merchant-stock-request", args=[request.pk])), "KAOS-L")
