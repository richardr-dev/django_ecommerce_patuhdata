"""Satuan barang. Stock is counted in pcs; Kabel also comes in a dus of 12.

Worked example: order 3 dus at 660.000 per dus → the purchase order holds 36 pcs at 55.000. Sell 2 dus at
1.200.000 per dus → the invoice reads 2 dus × 1.200.000 = 2.400.000 (+ PPN), the shelf loses 24 pcs and HPP
is 24 × 60.000 = 1.440.000. A credit note for 1 dus brings 12 pcs back and 720.000 back to Persediaan."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import purchasing
from apps.accounting.controls import _balances, invoice_open_balance, stock_movements
from apps.accounting.models import Supplier
from apps.catalog import units
from apps.catalog.models import ProductUnit, ProductVariant
from apps.orders.stock import record_opening_balances
from apps.tax.credit_notes import create_credit_note
from apps.tax.models import Invoice
from apps.tax.sales import create_sales_invoice, invoice_lines, update_sales_invoice

D = Decimal


class UnitTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.dus = units.save_unit(self.kabel, name="dus", factor=12, barcode="899000012")

    def sell(self, qty=2, unit="dus", price=1_200_000):
        return create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": qty, "unit": unit,
            "unit_price": D(price)}], buyer_name="Budi", customer=self.customer, on_credit=True)

    def checks(self):
        balances = _balances(self.store)
        return (invoice_open_balance(self.store, self.today, balances).status,
            stock_movements(self.store, self.today, balances).status)

    def test_units_are_checked(self):
        self.assertEqual(units.units_for(self.kabel), [("pcs", 1), ("dus", 12)])
        self.assertEqual((units.factor(self.kabel, "DUS"), units.factor(self.kabel, "pcs"),
            units.factor(self.kabel, "set")), (12, 1, 1))
        self.assertEqual(units.by_barcode(self.store, "899000012"), self.dus)
        for name, factor, words in (("dus", 6, "sudah ada"), ("pcs", 6, "satuan dasar"), ("pak", 1, "2 atau lebih")):
            with self.assertRaisesMessage(units.UnitError, words):
                units.save_unit(self.kabel, name=name, factor=factor)
        ProductVariant.objects.create(product=self.lampu, name="Putih", sku="LMP-P")
        with self.assertRaisesMessage(units.UnitError, "varian"):
            units.save_unit(self.lampu, name="dus", factor=10)

    def test_a_purchase_order_by_the_dus_counts_pcs(self):
        supplier = Supplier.objects.create(store=self.store, name="CV Sumber")
        po = purchasing.create_po(self.store, supplier=supplier, order_date=self.today,
            lines=[{"sku": "KBL", "qty": 3, "unit": "dus", "unit_cost": D(660_000)}])
        line = po.lines.get()
        self.assertEqual((line.quantity, line.unit_cost, line.ordered_in_unit), (36, D(55_000), "3 dus"))
        receipt = purchasing.receive_po(po, quantities={line.pk: 36}, received_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 136)
        self.assertEqual(receipt.value, D(1_980_000))

    def test_sell_by_the_dus_edit_and_credit(self):
        invoice = self.sell()
        item = invoice.items.get()
        self.assertEqual((item.quantity, item.unit, item.unit_factor, item.unit_price), (2, "dus", 12, D(1_200_000)))
        self.assertEqual((invoice.subtotal, invoice.total), (D(2_400_000), D(2_664_000)))
        self.assertEqual(self.on_hand(self.kabel), 76)
        order_item = invoice.order.items.get()
        self.assertEqual((order_item.quantity, order_item.unit_price, order_item.unit_cost), (24, D(100_000), D(60_000)))
        self.assertEqual(balance(self.store, "5100"), D(1_440_000))
        self.assertEqual(self.checks(), ("ok", "ok"))

        lines = invoice_lines(invoice)                          # saved unchanged: nothing moves
        invoice = update_sales_invoice(invoice, buyer_name="Budi", issued_on=invoice.issued_on,
            due_date=invoice.due_date, notes="", lines=lines)
        self.assertEqual(self.on_hand(self.kabel), 76)
        lines[0]["unit"] = "pcs"                                 # 2 dus → 2 pcs: the stock follows
        lines[0]["unit_price"] = D(100_000)
        invoice = update_sales_invoice(invoice, buyer_name="Budi", issued_on=invoice.issued_on,
            due_date=invoice.due_date, notes="", lines=lines)
        self.assertEqual((self.on_hand(self.kabel), invoice.subtotal), (98, D(200_000)))
        self.assertEqual(self.checks(), ("ok", "ok"))

    def test_a_credit_note_of_one_dus_restocks_twelve(self):
        invoice = self.sell()
        item = invoice.items.get()
        note = create_credit_note(invoice, lines=[{"item": item, "description": item.description, "quantity": 1,
            "unit_price": item.unit_price, "restock": True}], issued_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 88)
        self.assertEqual(note.items.get().unit_factor, 12)
        self.assertEqual(balance(self.store, "5100"), D(720_000))
        self.assertEqual(self.checks(), ("ok", "ok"))
        with self.assertRaisesMessage(Exception, "utuh"):
            create_credit_note(invoice, lines=[{"item": item, "description": item.description,
                "quantity": D("0.05"), "unit_price": item.unit_price, "restock": True}], issued_on=self.today)   # 0,6 pcs
        from apps.tax.credit_notes import void_credit_note
        void_credit_note(note)
        self.assertEqual(self.on_hand(self.kabel), 76)

    def test_the_invoice_form_and_the_units_page(self):
        self.client.force_login(self.owner)
        form = self.client.get(reverse("merchant-invoice-new"))
        kabel = next(row for row in form.context["catalog"] if row["id"] == str(self.kabel.pk))
        self.assertEqual((kabel["base"], kabel["units"]), ("pcs", [{"name": "dus", "factor": 12}]))
        self.client.post(reverse("merchant-invoice-new"), {"idem": "u1", "buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "payment_status": "credit", "channel": "manual",
            "product[]": [str(self.kabel.pk)], "description[]": [""], "qty[]": ["1"], "unit_price[]": ["1200000"],
            "line_discount[]": [""], "unit[]": ["dus"], "note[]": [""], "code[]": [""]})
        self.assertEqual(Invoice.objects.get(store=self.store).items.get().unit_factor, 12)
        self.assertEqual(self.on_hand(self.kabel), 88)
        page = self.client.get(reverse("merchant-units"))
        self.assertContains(page, "1 dus = 12 pcs")
        self.client.post(reverse("merchant-units"), {"action": "save", "product": str(self.lampu.pk), "name": "pak",
            "factor": "6"})
        self.assertTrue(ProductUnit.objects.filter(product=self.lampu, name="pak", factor=6).exists())
        self.client.post(reverse("merchant-units"), {"action": "base", "product": str(self.lampu.pk), "base_unit": "buah"})
        self.lampu.refresh_from_db()
        self.assertEqual(self.lampu.base_unit, "buah")
        self.client.post(reverse("merchant-units"), {"action": "delete", "product": str(self.kabel.pk),
            "unit": str(self.dus.pk)})
        self.assertEqual(units.units_for(self.kabel), [("pcs", 1)])   # no longer offered...
        self.assertEqual(units.factor(self.kabel, "dus"), 12)           # ...but an old "dus" line still means 12
