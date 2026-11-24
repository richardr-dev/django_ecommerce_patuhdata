"""Editing an invoice keeps the books tied: the location it was sold from, no stale rounding, and the
per-invoice receivable check counting the reversal an edit leaves."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.controls import _balances, invoice_open_balance
from apps.accounting.models import JournalEntry
from apps.catalog.models import InventoryLevel, Warehouse
from apps.orders.stock import record_opening_balances
from apps.tax.models import Invoice
from apps.tax.sales import create_sales_invoice, from_inclusive, invoice_lines, update_sales_invoice

D = Decimal


class InvoiceEditTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.gudang = Warehouse.objects.get(store=self.store)
        self.toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko Depan", priority=200)
        InventoryLevel.objects.create(warehouse=self.toko, product=self.kabel, on_hand=5)

    def level(self, warehouse):
        return InventoryLevel.objects.get(product=self.kabel, warehouse=warehouse).on_hand

    def edit(self, invoice, qty, **extra):
        lines = invoice_lines(invoice)
        lines[0]["qty"] = qty
        return update_sales_invoice(invoice, buyer_name="Budi", issued_on=invoice.issued_on,
            due_date=invoice.due_date, notes="", lines=lines, **extra)

    def check(self):
        return invoice_open_balance(self.store, self.today, _balances(self.store))

    def test_an_edit_takes_the_goods_from_the_location_sold_from(self):
        invoice = create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": 2,
            "unit_price": D(100_000)}], buyer_name="Budi", customer=self.customer, on_credit=True,
            warehouse=self.toko)
        self.assertEqual(invoice.warehouse, self.toko)
        self.assertEqual((self.level(self.gudang), self.level(self.toko)), (100, 3))
        self.edit(invoice, 4)
        self.assertEqual((self.level(self.gudang), self.level(self.toko)), (100, 1))
        with self.assertRaisesMessage(Exception, "Toko Depan"):   # Gudang's 100 do not count
            self.edit(Invoice.objects.get(pk=invoice.pk), 7)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk])), "Dijual dari")

    def test_an_edit_drops_the_rounding_of_inclusive_prices(self):
        lines, discount, rounding = from_inclusive(self.store, [{"product": self.kabel, "qty": 3,
            "unit_price": D(10_000), "discount": D(1_500)}], D(2_000))
        invoice = create_sales_invoice(self.store, lines=lines, discount=discount, rounding=rounding,
            buyer_name="Budi", customer=self.customer, on_credit=True)
        self.assertEqual(invoice.total, D(26_500))
        Invoice.objects.filter(pk=invoice.pk).update(rounding=D("0.40"), total=invoice.total)  # as a register leaves it
        invoice = self.edit(invoice, 1)
        self.assertEqual(invoice.rounding, 0)
        self.assertEqual(invoice.total, invoice.subtotal - invoice.discount + invoice.tax)
        self.assertEqual(self.check().status, "ok")

    def form(self, url, qty, inclusive=True, **extra):
        data = {"idem": "e-incl", "buyer_name": "Budi", "issued_on": self.today.isoformat(),
            "payment_status": "credit", "channel": "manual", "product[]": [str(self.kabel.pk)],
            "description[]": [""], "qty[]": [str(qty)], "unit_price[]": ["10000"], "line_discount[]": ["1500"],
            "unit[]": [""], "note[]": [""], "code[]": [""], "discount": "2000", **extra}
        if inclusive:
            data["prices_include_tax"] = "1"
        return self.client.post(url, data)

    def test_an_inclusive_invoice_is_edited_with_the_prices_typed(self):
        """3 × 10.000 with PPN inside, line discount 1.500, invoice discount 2.000: total 26.500. The edit
        form shows those figures again; 4 units make 36.500 with PPN still inside."""
        self.client.force_login(self.owner)
        self.form(reverse("merchant-invoice-new"), 3)
        invoice = Invoice.objects.get(store=self.store)
        self.assertEqual((invoice.total, invoice.prices_include_tax), (D(26_500), True))
        edit = reverse("merchant-invoice-edit", args=[invoice.pk])
        page = self.client.get(edit)
        self.assertContains(page, 'name="prices_include_tax" value="1" id="f-incl" class="mt-0.5 h-4 w-4 rounded" checked')
        self.assertEqual(page.context["initial"]["lines"][0]["price"], "10000")
        self.assertEqual(page.context["initial"]["discount"], "2000")
        entries = JournalEntry.objects.filter(store=self.store).count()
        self.form(edit, 3)                                   # saved unchanged: nothing is rebooked
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).total, D(26_500))
        self.assertEqual(JournalEntry.objects.filter(store=self.store).count(), entries)
        self.form(edit, 4)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, D(36_500))
        self.assertEqual(invoice.total, invoice.subtotal - invoice.discount + invoice.tax + invoice.rounding)
        self.assertEqual(self.check().status, "ok")
        self.form(edit, 4, inclusive=False)                  # untick: the prices are before PPN
        invoice.refresh_from_db()
        self.assertFalse(invoice.prices_include_tax)
        self.assertEqual(invoice.rounding, 0)
        self.assertEqual(invoice.total, D("40515"))         # (40.000 − 1.500 − 2.000) × 1,11
        self.assertEqual(self.check().status, "ok")

    def test_the_receivable_check_counts_the_reversal_of_an_edit(self):
        invoice = self.invoice()
        self.edit(invoice, 3)
        cash = self.invoice(credit=False)
        update_sales_invoice(cash, buyer_name="Budi", issued_on=self.today.replace(day=1)
            if self.today.day > 1 else self.today, due_date=None, notes="")
        self.assertEqual(self.check().status, "ok", self.check().items)
