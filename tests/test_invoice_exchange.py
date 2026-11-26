"""Tukar barang on an invoice: the original stays as issued, a credit note takes the line back, a replacement
invoice sells the new item and is paid from the credit as far as it reaches."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.catalog.models import InventoryLevel, Product
from apps.tax.exchanges import ExchangeError, exchange_line
from apps.tax.models import CreditNote, Invoice, InvoiceExchange
from apps.tax.receipts import credit_balance

D = Decimal


class ExchangeBase(Base):
    def setUp(self):
        super().setUp()
        self.kabel_l = Product.objects.create(store=self.store, sku="KBL-L", name="Kabel L", slug="kabel-l",
            price=100_000, cost_price=60_000)
        InventoryLevel.objects.create(warehouse=InventoryLevel.objects.first().warehouse, product=self.kabel_l,
            on_hand=10)

    def swap(self, invoice, **kwargs):
        item = invoice.items.get(product=self.kabel)
        return exchange_line(invoice, item=item, quantity=kwargs.pop("quantity", 1),
            replacement_code=kwargs.pop("code", "KBL-L"), **kwargs)


class ExchangeTests(ExchangeBase):
    def test_same_price_on_an_unpaid_invoice_moves_the_debt_to_the_replacement(self):
        invoice = self.invoice(kabel=2)                         # 2 × 100.000 + PPN = 222.000, unpaid
        swap = self.swap(invoice)
        invoice.refresh_from_db()
        new = swap.replacement_invoice
        self.assertEqual((swap.credit_note.total, new.total), (D(111_000), D(111_000)))
        self.assertEqual(invoice.amount_due + new.amount_due, D(222_000))   # the buyer owes what they owed
        self.assertEqual((self.on_hand(self.kabel), self.on_hand(self.kabel_l)), (100 - 2 + 1, 9))
        self.assertEqual(invoice.items.count(), 1)              # the original is never edited
        self.assertEqual(invoice.exchange_state, "Sebagian ditukar")
        self.assertIn(invoice.number, new.notes)

    def test_a_paid_invoice_pays_the_replacement_from_the_credit(self):
        invoice = self.invoice(kabel=1, credit=False)          # paid 111.000
        new = self.swap(invoice).replacement_invoice
        new.refresh_from_db()
        self.assertEqual((new.amount_due, new.status), (0, Invoice.STATUS_PAID))
        self.assertEqual(credit_balance(self.store, self.customer, "Budi"), 0)
        invoice.refresh_from_db()
        self.assertEqual(invoice.exchange_state, "Ditukar")
        self.assertFalse(invoice.fully_returned)

    def test_a_dearer_replacement_leaves_the_difference_to_collect(self):
        invoice = self.invoice(kabel=1, credit=False)
        new = self.swap(invoice, unit_price="120000").replacement_invoice
        new.refresh_from_db()
        self.assertEqual((new.total, new.amount_due), (D(133_200), D(22_200)))

    def test_a_cheaper_replacement_leaves_customer_credit(self):
        invoice = self.invoice(kabel=1, credit=False)
        self.swap(invoice, unit_price="80000")
        self.assertEqual(credit_balance(self.store, self.customer, "Budi"), D(22_200))

    def test_a_damaged_unit_does_not_come_back_to_the_shelf(self):
        invoice = self.invoice(kabel=1)
        self.swap(invoice, condition="damaged")
        self.assertEqual(self.on_hand(self.kabel), 99)
        self.assertFalse(CreditNote.objects.get().items.get().restock)

    def test_refusals_write_nothing(self):
        invoice = self.invoice(kabel=1)
        for kwargs, message in (({"quantity": 2}, "1 sampai 1"), ({"code": "KBL"}, "sama dengan"),
                ({"code": "NOPE"}, "tidak ditemukan")):
            with self.assertRaisesRegex(ExchangeError, message):
                self.swap(invoice, **kwargs)
        self.assertEqual((CreditNote.objects.count(), Invoice.objects.count(), InvoiceExchange.objects.count()),
            (0, 1, 0))


class ExchangePageTests(ExchangeBase):
    def test_the_dialog_swaps_and_every_document_shows_the_others(self):
        self.client.force_login(self.owner)
        invoice = self.invoice(kabel=1, credit=False)
        page = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]))
        self.assertContains(page, "Tukar barang")
        self.assertContains(page, "KBL-L")                      # offered in the SKU list
        idem = page.context["idem_key"]
        data = {"item": invoice.items.get().pk, "quantity": "1", "replacement": "KBL-L", "condition": "good",
            "issued_on": self.today.isoformat(), "idem": idem}
        url = reverse("merchant-invoice-exchange", args=[invoice.pk])
        self.client.post(url, data)
        self.client.post(url, data)                              # a double click makes nothing twice
        swap = InvoiceExchange.objects.get()
        old = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]))
        self.assertContains(old, "Ditukar 1 dengan")
        self.assertContains(old, swap.replacement_invoice.number)
        self.assertContains(old, "DITUKAR")
        self.assertNotContains(old, "<s>")                       # never struck through
        new = self.client.get(reverse("merchant-invoice-detail", args=[swap.replacement_invoice.pk]))
        self.assertContains(new, "Invoice pengganti")
        self.assertContains(new, invoice.number)
        for name in ("merchant-invoice-pdf",):
            self.assertEqual(self.client.get(reverse(name, args=[invoice.pk])).status_code, 200)
        from apps.tax import sharing
        public = self.client.get(reverse("invoice-public", args=[sharing.ensure_token(invoice)]))
        self.assertContains(public, "DITUKAR")

    def test_an_error_is_shown_and_nothing_is_saved(self):
        self.client.force_login(self.owner)
        invoice = self.invoice(kabel=1)
        page = self.client.post(reverse("merchant-invoice-exchange", args=[invoice.pk]), {"item":
            invoice.items.get().pk, "quantity": "1", "replacement": "NOPE", "idem": "k1"}, follow=True)
        self.assertContains(page, "tidak ditemukan")
        self.assertFalse(InvoiceExchange.objects.exists())
