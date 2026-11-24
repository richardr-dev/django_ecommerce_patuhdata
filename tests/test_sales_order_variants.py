"""Sizes on a sales order, and a surat jalan sent from a chosen warehouse.

Kaos M: 10 in Gudang, 5 in Toko (cost 40.000); Kaos L: 4 in Gudang (cost 45.000, sold at 90.000). An order for
6 M and 2 L ships 5 M from Toko and 2 L, leaving 1 M on backorder. 1410 holds 5 × 40.000 + 2 × 45.000 = 290.000
until the invoice moves it to HPP; cancelling the surat jalan puts the M units back in Toko."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting.controls import _balances, delivered_tie, stock_movements
from apps.catalog.models import Product, ProductVariant, Warehouse
from apps.orders.stock import receive_goods, record_opening_balances
from apps.orders.variant_stock import at
from apps.tax import sales_orders
from apps.tax.models import SalesOrder

D = Decimal


class SalesOrderVariantTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.gudang = Warehouse.objects.get(store=self.store)
        self.toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko Depan", priority=200)
        self.kaos = Product.objects.create(store=self.store, sku="KAOS", name="Kaos", slug="kaos", price=80_000)
        self.m = ProductVariant.objects.create(product=self.kaos, name="M", sku="KAOS-M", cost_price=D(40_000))
        self.l = ProductVariant.objects.create(product=self.kaos, name="L", sku="KAOS-L", price=D(90_000),
            cost_price=D(45_000))
        for variant, qty, place in ((self.m, 10, self.gudang), (self.m, 5, self.toko), (self.l, 4, self.gudang)):
            receive_goods(store=self.store, product=self.kaos, variant=variant, quantity=qty,
                unit_cost=variant.cost_price, warehouse=place)

    def where(self, variant):
        variant.refresh_from_db()
        return variant.stock, at(variant, self.gudang), at(variant, self.toko)

    def checks(self):
        balances = _balances(self.store)
        return (delivered_tie(self.store, self.today, balances).status,
            stock_movements(self.store, self.today, balances).status)

    def order(self):
        return sales_orders.create_sales_order(self.store, lines=[
            {"variant": self.m, "qty": 6, "unit_price": D(80_000)},
            {"variant": self.l, "qty": 2, "unit_price": D(90_000)}], buyer_name="Budi", customer=self.customer)

    def test_order_ship_from_a_location_invoice_and_cancel(self):
        order = self.order()
        m_line, l_line = order.lines.order_by("sku").reverse()
        self.assertEqual([(line.variant, line.sku, line.description) for line in (m_line, l_line)],
            [(self.m, "KAOS-M", "Kaos / M"), (self.l, "KAOS-L", "Kaos / L")])
        with self.assertRaises(sales_orders.SalesOrderError):        # Toko holds no L
            sales_orders.create_delivery(order, quantities={m_line.pk: 5, l_line.pk: 2}, warehouse=self.toko)
        self.assertEqual(self.where(self.l), (4, 4, 0))
        first = sales_orders.create_delivery(order, quantities={m_line.pk: 5}, warehouse=self.toko)
        second = sales_orders.create_delivery(order, quantities={l_line.pk: 2})
        self.assertEqual((self.where(self.m), self.where(self.l)), ((10, 10, 0), (2, 2, 0)))
        self.assertEqual(first.warehouse, self.toko)
        self.assertEqual(balance(self.store, "1410"), D(290_000))
        order.refresh_from_db()
        self.assertEqual((order.status, sales_orders.backorder(self.store).get().remaining),
            (SalesOrder.STATUS_PARTIAL, 1))
        self.assertEqual(self.checks(), ("ok", "ok"))

        invoice = sales_orders.invoice_deliveries(order, [second])
        item = invoice.items.get()
        self.assertEqual((item.variant, item.sku, invoice.subtotal), (self.l, "KAOS-L", D(180_000)))
        self.assertEqual(invoice.order.items.get().unit_cost, D(45_000))   # the L's own cost, not the M's
        self.assertEqual(balance(self.store, "1410"), D(200_000))
        self.assertEqual(self.checks(), ("ok", "ok"))

        sales_orders.cancel_delivery(first)
        self.assertEqual(self.where(self.m), (15, 10, 5))           # back on Toko's shelf
        self.assertEqual(balance(self.store, "1410"), D(0))
        self.assertEqual(self.checks(), ("ok", "ok"))

    def test_the_pages_order_a_size_and_ship_it_from_a_location(self):
        self.client.force_login(self.owner)
        form = self.client.get(reverse("merchant-sales-order-new"))
        offered = {row["id"]: row for row in form.context["products"]}
        self.assertNotIn(str(self.kaos.pk), offered)                 # ordered by size, not the parent
        self.assertEqual(offered[f"v{self.l.pk}"]["name"], "Kaos / L")
        self.assertContains(form, f'value="v{self.l.pk}"')
        self.client.post(reverse("merchant-sales-order-new"), {"buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "product[]": [f"v{self.m.pk}"], "qty[]": ["3"],
            "unit_price[]": ["80000"]})
        order = SalesOrder.objects.get(store=self.store)
        line = order.lines.get()
        self.assertEqual(line.variant, self.m)
        detail = self.client.get(reverse("merchant-sales-order-detail", args=[order.pk]))
        self.assertContains(detail, "Kirim dari gudang")
        self.assertEqual(detail.context["rows"][0]["shelf"], 15)
        self.client.post(reverse("merchant-sales-order-detail", args=[order.pk]), {"action": "deliver",
            f"qty_{line.pk}": "3", "warehouse": str(self.toko.pk), "delivered_on": self.today.isoformat()})
        self.assertEqual(self.where(self.m), (12, 10, 2))
        delivery = order.deliveries.get()
        page = self.client.get(reverse("merchant-delivery-detail", args=[delivery.pk]))
        self.assertContains(page, "Kaos / M")
        self.assertContains(page, "dikirim dari <b>Toko Depan</b>")
        self.assertEqual(len(page.context["journal"]), 1)            # the 1410 / Persediaan entry is shown
        self.assertContains(self.client.get(reverse("merchant-delivery-print", args=[delivery.pk])), "KAOS-M")


class QuoteVariantTests(SalesOrderVariantTests):
    """A quote for 2 Kaos L keeps the size on its way to an invoice, or to a sales order and its surat jalan."""

    def quote(self):
        from apps.tax import quotes
        return quotes.save_quote(self.store, lines=[{"product": self.kaos, "variant": self.l, "qty": 2,
            "unit_price": D(90_000)}], buyer_name="Budi", customer=self.customer)

    def test_a_quote_becomes_an_invoice_for_that_size(self):
        from apps.tax import quotes
        quote = self.quote()
        item = quote.items.get()
        self.assertEqual((item.variant, item.sku, item.description, item.pick_id),
            (self.l, "KAOS-L", "Kaos / L", f"v{self.l.pk}"))
        invoice = quotes.convert_to_invoice(quote)
        self.assertEqual(invoice.items.get().variant, self.l)
        self.assertEqual(self.where(self.l), (2, 2, 0))

    def test_a_quote_becomes_an_order_and_ships_that_size(self):
        order = sales_orders.from_quote(self.quote())
        line = order.lines.get()
        self.assertEqual(line.variant, self.l)
        sales_orders.create_delivery(order, quantities={line.pk: 2})
        self.assertEqual(self.where(self.l), (2, 2, 0))

    def test_the_quote_form_offers_sizes_and_keeps_them_on_edit(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-quote-new"), {"buyer_name": "Budi", "issued_on": self.today.isoformat(),
            "product[]": [f"v{self.l.pk}"], "qty[]": ["2"], "unit_price[]": ["90000"]})
        from apps.tax.models import Quote
        quote = Quote.objects.get(store=self.store)
        self.assertEqual(quote.items.get().variant, self.l)
        page = self.client.get(reverse("merchant-quote-edit", args=[quote.pk]))
        self.assertContains(page, f'value="v{self.l.pk}" data-price="90000" selected')
