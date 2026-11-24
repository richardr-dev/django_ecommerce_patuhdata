"""Variant stock per warehouse: ``ProductVariant.stock`` stays the store-wide total; ``VariantStock`` rows say
where the units are and always add up to it.

Kaos M: 10 received into Gudang, 5 into Toko (15 in all). A sale of 12 with no location takes Gudang's 10 first,
then 2 from Toko. A transfer of 3 Toko → Gudang leaves the total alone. A sale from Toko takes only Toko's units
and comes back to Toko when it is cancelled."""
import uuid
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import stock_adjustments
from apps.accounting.controls import _balances, stock_movements
from apps.accounting.inventory_reports import mutation
from apps.catalog.models import Product, ProductVariant, VariantStock, Warehouse
from apps.orders.inventory import (
    commit_order_inventory,
    deduct_invoice_stock,
    reserve_variant,
    return_order_inventory,
)
from apps.orders.models import InventoryMovement, Order, OrderItem
from apps.orders.stock import (
    StockError,
    receive_goods,
    record_opening_balances,
    stock_audit,
    stock_out,
    transfer_stock,
)
from apps.orders.variant_stock import at, locations

D = Decimal


class VariantWarehouseTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)  # Base made its levels without movements
        self.gudang = Warehouse.objects.get(store=self.store)
        self.toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko Depan", priority=200)
        self.kaos = Product.objects.create(store=self.store, sku="KAOS", name="Kaos", slug="kaos", price=80_000)
        self.m = ProductVariant.objects.create(product=self.kaos, name="M", sku="KAOS-M", cost_price=D(40_000))
        receive_goods(store=self.store, product=self.kaos, variant=self.m, quantity=10, unit_cost=D(40_000),
            warehouse=self.gudang)
        receive_goods(store=self.store, product=self.kaos, variant=self.m, quantity=5, unit_cost=D(40_000),
            warehouse=self.toko)
        self.m.refresh_from_db()

    def where(self):
        self.m.refresh_from_db()
        return self.m.stock, at(self.m, self.gudang), at(self.m, self.toko)

    def order(self, quantity):
        order = Order.objects.create(store=self.store, customer_name="Budi", phone="0812", shipping_address="x",
            subtotal=D(80_000) * quantity, total=D(80_000) * quantity, status="paid", idempotency_key=uuid.uuid4().hex)
        OrderItem.objects.create(order=order, product=self.kaos, variant=self.m, name="Kaos M", sku="KAOS-M",
            unit_price=80_000, quantity=quantity, line_total=80_000 * quantity)
        return order

    def test_receipts_land_where_they_were_received(self):
        self.assertEqual(self.where(), (15, 10, 5))
        moves = InventoryMovement.objects.filter(variant=self.m, kind="receipt").order_by("id")
        self.assertEqual([m.warehouse_id for m in moves], [self.gudang.pk, self.toko.pk])

    def test_a_sale_without_a_location_draws_in_priority_order(self):
        order = self.order(12)
        item = order.items.get()
        reserve_variant(item, self.m, 12)
        commit_order_inventory(order)
        self.assertEqual(self.where(), (3, 0, 3))
        commits = InventoryMovement.objects.filter(order_item=item, kind="commit").order_by("id")
        self.assertEqual([(m.warehouse_id, m.previous_on_hand, m.new_on_hand) for m in commits],
            [(self.gudang.pk, 15, 5), (self.toko.pk, 5, 3)])
        return_order_inventory(order)                       # back where each unit came from
        self.assertEqual(self.where(), (15, 10, 5))
        self.assertEqual(stock_audit(self.store), [])

    def test_a_sale_from_a_location_takes_only_its_shelf_and_returns_there(self):
        order = self.order(4)
        self.assertEqual(deduct_invoice_stock(order, warehouse=self.toko), [])
        self.assertEqual(self.where(), (11, 10, 1))
        too_many = self.order(2)
        self.assertEqual(deduct_invoice_stock(too_many, warehouse=self.toko), [("KAOS-M", 2, 1)])
        return_order_inventory(order)
        self.assertEqual(self.where(), (15, 10, 5))

    def test_transfer_moves_units_and_keeps_the_total(self):
        transfer_stock(store=self.store, product=self.kaos, variant=self.m, quantity=3, source=self.toko,
            target=self.gudang)
        self.assertEqual(self.where(), (15, 13, 2))
        with self.assertRaises(StockError):
            transfer_stock(store=self.store, product=self.kaos, variant=self.m, quantity=3, source=self.toko,
                target=self.gudang)
        with self.assertRaisesMessage(StockError, "Toko Depan"):
            stock_out(store=self.store, product=self.kaos, variant=self.m, quantity=3, kind="adjust", reason="x",
                warehouse=self.toko)
        self.assertEqual(stock_audit(self.store), [])
        report = mutation(self.store, self.today, self.today, warehouse=self.toko)
        row = next(r for r in report["rows"] if r["sku"] == "KAOS-M")
        self.assertEqual((row["purchases"], row["transfers"], row["closing"]), (5, -3, 2))

    def test_old_stock_without_rows_counts_in_the_first_warehouse(self):
        old = ProductVariant.objects.create(product=self.kaos, name="L", sku="KAOS-L", stock=7, cost_price=D(40_000))
        self.assertEqual((at(old, self.gudang), at(old, self.toko)), (7, 0))
        stock_out(store=self.store, product=self.kaos, variant=old, quantity=2, kind="adjust", reason="x")
        old.refresh_from_db()
        self.assertEqual(dict(locations(old)), {self.gudang.pk: 5})
        self.assertEqual(VariantStock.objects.get(variant=old).on_hand, 5)

    def test_penyesuaian_and_kelola_stok_per_location(self):
        stock_adjustments.post_adjustment(self.store, warehouse=self.toko, adjusted_on=self.today, reason="damaged",
            lines=[{"product": self.kaos, "variant": self.m, "quantity": -2}])
        self.assertEqual(self.where(), (13, 10, 3))
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-stock"))
        self.assertContains(page, "Toko Depan · KAOS-M")
        self.assertContains(page, "Pindah gudang")
        self.client.post(reverse("merchant-stock"), {"target_type": "variant", "target_id": self.m.pk,
            "warehouse": self.toko.pk, "stock": "1", "safety_stock": "0", "reason_code": "lost"})
        self.assertEqual(self.where(), (11, 10, 1))
        self.client.post(reverse("merchant-stock"), {"action": "move", "target_type": "variant",
            "target_id": self.m.pk, "source": self.gudang.pk, "target": self.toko.pk, "quantity": "4"})
        self.assertEqual(self.where(), (11, 6, 5))
        self.client.post(reverse("merchant-stock"), {"action": "move", "target_type": "product",
            "target_id": self.kabel.pk, "source": self.gudang.pk, "target": self.toko.pk, "quantity": "10"})
        self.assertEqual(self.on_hand_at(self.kabel, self.toko), 10)

    def test_rows_that_do_not_add_up_are_flagged(self):
        self.assertEqual(stock_movements(self.store, self.today, _balances(self.store)).status, "ok")
        ProductVariant.objects.filter(pk=self.m.pk).update(stock=20)   # changed outside any movement
        check = stock_movements(self.store, self.today, _balances(self.store))
        self.assertEqual(check.status, "warn")

    def on_hand_at(self, product, warehouse):
        from apps.catalog.models import InventoryLevel
        return InventoryLevel.objects.get(product=product, warehouse=warehouse).on_hand
