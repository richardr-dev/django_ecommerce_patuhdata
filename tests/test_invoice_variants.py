"""Sizes and colours on a sales invoice. Kaos has M (10 in Gudang, 5 in Toko, cost 40.000) and L (4 in Gudang,
cost 45.000), sold at 80.000 (L at 90.000). A variant line takes that variant's units (from the location
chosen, when one is), books its own cost as HPP, comes back on an edit or void, and a credit note restocks the
size returned. A variant is never sold short."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.controls import _balances, invoice_open_balance, stock_movements
from apps.catalog.models import Product, ProductVariant, Warehouse
from apps.orders.stock import receive_goods, record_opening_balances
from apps.orders.variant_stock import at
from apps.tax import invoice_batch as batch
from apps.tax.credit_notes import create_credit_note
from apps.tax.models import Invoice
from apps.tax.posting import void_invoice
from apps.tax.sales import StockShortage, create_sales_invoice, invoice_lines, update_sales_invoice

D = Decimal


class InvoiceVariantTests(Base):
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

    def stock(self, variant):
        variant.refresh_from_db()
        return variant.stock

    def sell(self, *lines, **extra):
        return create_sales_invoice(self.store, lines=[{"variant": v, "qty": q, "unit_price": v.effective_price}
            for v, q in lines], buyer_name="Budi", customer=self.customer, on_credit=True, **extra)

    def checks(self):
        balances = _balances(self.store)
        return (invoice_open_balance(self.store, self.today, balances).status,
            stock_movements(self.store, self.today, balances).status)

    def test_a_variant_line_takes_its_own_stock_and_cost(self):
        invoice = self.sell((self.m, 3), (self.l, 1))
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (12, 3))
        items = list(invoice.items.order_by("sku"))
        self.assertEqual([(i.variant, i.sku, i.description) for i in items],
            [(self.l, "KAOS-L", "Kaos / L"), (self.m, "KAOS-M", "Kaos / M")])
        self.assertEqual(invoice.subtotal, D(330_000))
        hpp = sum(item.unit_cost * item.quantity for item in invoice.order.items.all())
        self.assertEqual(hpp, D(165_000))                          # 3 × 40.000 + 45.000
        self.assertEqual(self.checks(), ("ok", "ok"))

    def test_a_variant_is_never_sold_short_and_a_location_counts_alone(self):
        self.store.allow_negative_stock = True
        self.store.save()
        with self.assertRaisesMessage(StockShortage, "KAOS-L"):
            self.sell((self.l, 5))
        with self.assertRaisesMessage(StockShortage, "Toko Depan"):   # Toko holds 5 M, Gudang's 10 do not count
            self.sell((self.m, 6), warehouse=self.toko)
        self.sell((self.m, 5), warehouse=self.toko)
        self.m.refresh_from_db()                                  # at() reads the stock held on the object
        self.assertEqual((at(self.m, self.toko), at(self.m, self.gudang)), (0, 10))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_an_edit_swaps_sizes_and_a_void_puts_them_back(self):
        invoice = self.sell((self.m, 2))
        lines = invoice_lines(invoice)
        lines[0].update(variant=self.l, description="", unit_price=D(90_000))
        invoice = update_sales_invoice(invoice, buyer_name="Budi", issued_on=invoice.issued_on,
            due_date=invoice.due_date, notes="", lines=lines)
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (15, 2))
        self.assertEqual(invoice.items.get().variant, self.l)
        self.assertEqual(self.checks(), ("ok", "ok"))
        void_invoice(invoice)
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (15, 4))

    def test_a_credit_note_restocks_the_size_returned(self):
        invoice = self.sell((self.m, 2), (self.l, 2))
        item = invoice.items.get(variant=self.l)
        create_credit_note(invoice, lines=[{"item": item, "description": item.description, "quantity": 1,
            "unit_price": item.unit_price, "restock": True}], issued_on=self.today, reason="Kebesaran")
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (13, 3))

    def test_the_form_lists_sizes_and_sells_one(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-invoice-new"))
        catalog = {row["id"]: row for row in page.context["catalog"]}
        self.assertFalse(catalog[str(self.kaos.pk)]["pick"])        # the parent is sold through its sizes
        self.assertEqual(catalog[f"v{self.l.pk}"]["label"], "KAOS-L — Kaos / L")
        self.assertEqual((catalog[f"v{self.l.pk}"]["price"], catalog[f"v{self.l.pk}"]["stock"]), ("90000", 4))
        self.client.post(reverse("merchant-invoice-new"), {"idem": "var-1", "buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "payment_status": "credit", "channel": "manual",
            "product[]": [f"v{self.l.pk}"], "description[]": [""], "qty[]": ["2"], "unit_price[]": ["90000"],
            "line_discount[]": [""], "unit[]": [""], "note[]": [""], "code[]": [""]})
        invoice = Invoice.objects.get(store=self.store)
        self.assertEqual((invoice.items.get().variant, self.stock(self.l)), (self.l, 2))
        edit = self.client.get(reverse("merchant-invoice-edit", args=[invoice.pk]))
        self.assertEqual(edit.context["initial"]["lines"][0]["product_id"], f"v{self.l.pk}")
        page = self.client.post(reverse("merchant-invoice-new"), {"idem": "var-2", "buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "payment_status": "credit", "channel": "manual",
            "product[]": ["v999999"], "description[]": [""], "qty[]": ["1"], "unit_price[]": ["1"],
            "line_discount[]": [""], "unit[]": [""], "note[]": [""], "code[]": [""]})
        self.assertContains(page, "Varian produk tidak ditemukan")

    def test_the_batch_takes_a_variant_sku_and_refuses_the_parent(self):
        plans = batch.parse_paste(self.store, "A;Budi;;;KAOS-L;2;;\nB;Budi;;;KAOS;1;;\n")
        self.assertTrue(plans[0].ok)
        self.assertEqual(plans[0].total, D(199_800))                 # 2 × 90.000 × 1,11
        self.assertIn("SKU variannya", plans[1].errors[0])
        batch.create_all(self.store, plans[:1])
        self.assertEqual(self.stock(self.l), 2)
