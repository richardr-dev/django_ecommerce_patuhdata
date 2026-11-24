"""A sales order reserves the stock it promised.

Kabel: 100 on the shelf. An order for 30 holds 30, so an invoice for 80 is refused (70 free). Shipping 10 releases
those 10 before taking them: 90 on the shelf, 20 still held. Closing the order frees the rest. An order for 150
holds the 100 there are; 60 more arrive and "Cadangkan stok" holds 50 of them."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.controls import _balances, stock_movements
from apps.catalog.models import InventoryLevel, Product, ProductVariant
from apps.orders.stock import receive_goods, record_opening_balances
from apps.tax import sales_orders
from apps.tax.sales import StockShortage, create_sales_invoice

D = Decimal


class ReservationTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)

    def order(self, qty, product=None, variant=None):
        return sales_orders.create_sales_order(self.store, lines=[{"product": product or self.kabel, "variant": variant,
            "qty": qty, "unit_price": D(100_000)}], buyer_name="Budi", customer=self.customer)

    def level(self):
        level = InventoryLevel.objects.get(product=self.kabel)
        return level.on_hand, level.reserved

    def test_an_order_holds_what_it_promised(self):
        order = self.order(30)
        line = order.lines.get()
        self.assertEqual((self.level(), line.reserved_quantity), ((100, 30), 30))
        with self.assertRaises(StockShortage):
            create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": 80, "unit_price": D(100_000)}],
                buyer_name="Ani", on_credit=True)
        delivery = sales_orders.create_delivery(order, quantities={line.pk: 10})
        self.assertEqual((self.level(), line.reserved_quantity), ((90, 20), 20))
        sales_orders.cancel_delivery(delivery)                      # back on the shelf, and held again
        self.assertEqual((self.level(), line.reserved_quantity), ((100, 30), 30))
        sales_orders.close_order(order)
        self.assertEqual(self.level(), (100, 0))
        self.assertEqual(stock_movements(self.store, self.today, _balances(self.store)).status, "ok")

    def test_short_stock_holds_what_there_is_and_tops_up(self):
        order = self.order(150)
        line = order.lines.get()
        self.assertEqual(line.reserved_quantity, 100)
        receive_goods(store=self.store, product=self.kabel, quantity=60, unit_cost=D(60_000))
        self.assertEqual(sales_orders.reserve(order), 50)
        self.assertEqual((self.level(), line.reserved_quantity), ((160, 150), 150))
        self.assertEqual(sales_orders.reserve(order), 0)            # nothing more is owed

    def test_a_variant_order_holds_that_size(self):
        kaos = Product.objects.create(store=self.store, sku="KAOS", name="Kaos", slug="kaos", price=80_000)
        m = ProductVariant.objects.create(product=kaos, name="M", sku="KAOS-M", cost_price=D(40_000))
        receive_goods(store=self.store, product=kaos, variant=m, quantity=5, unit_cost=D(40_000))
        order = self.order(3, product=kaos, variant=m)
        m.refresh_from_db()
        self.assertEqual((m.stock, m.reserved), (5, 3))
        line = order.lines.get()
        sales_orders.create_delivery(order, quantities={line.pk: 3})
        m.refresh_from_db()
        self.assertEqual((m.stock, m.reserved), (2, 0))

    def test_the_page_shows_and_tops_up(self):
        order = self.order(150)
        receive_goods(store=self.store, product=self.kabel, quantity=10, unit_cost=D(60_000))
        self.client.force_login(self.owner)
        url = reverse("merchant-sales-order-detail", args=[order.pk])
        self.assertEqual(self.client.get(url).context["rows"][0]["held"], 100)
        self.client.post(url, {"action": "reserve"})
        self.assertEqual(self.client.get(url).context["rows"][0]["held"], 110)
        self.assertContains(self.client.get(url), "Cadangkan stok")
