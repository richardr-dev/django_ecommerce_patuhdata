"""Invoice gabungan: several invoices printed as one bill; books nothing."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import bank
from apps.accounting.models import JournalEntry
from apps.tax.combined import CombinedInvoiceError, combine
from apps.tax.models import Invoice


class CombinedTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.a = self.invoice(kabel=2, credit=True, due_days=14)
        self.b = self.invoice(kabel=3, credit=True, due_days=14)

    def test_products_are_summed_and_totals_add_up(self):
        bill = combine(self.store, [self.a, self.b])
        self.assertEqual(bill.totals["total"], self.a.total + self.b.total)
        self.assertEqual(bill.totals["due"], self.a.amount_due + self.b.amount_due)
        self.assertEqual(sum(p["quantity"] for p in bill.products), Decimal(5))

    def test_void_invoice_is_left_out_and_none_left_is_an_error(self):
        Invoice.objects.filter(pk=self.b.pk).update(status=Invoice.STATUS_VOID)
        self.b.refresh_from_db()
        bill = combine(self.store, [self.a, self.b])
        self.assertEqual(bill.left_out, [self.b])
        with self.assertRaises(CombinedInvoiceError):
            combine(self.store, [self.b])

    def test_page_shows_bank_account_and_books_nothing(self):
        account = bank.bank_accounts(self.store)[0]
        account.bank_name, account.account_number = "BCA", "123-456-7890"
        account.save()
        before = JournalEntry.objects.count()
        ids = f"{self.a.pk},{self.b.pk}"
        page = self.client.get(reverse("merchant-invoices-combined"), {"ids": ids, "view": 1})
        self.assertContains(page, "INVOICE GABUNGAN")
        self.assertContains(page, self.a.number)
        self.assertContains(page, "123-456-7890")
        self.assertEqual(JournalEntry.objects.count(), before)

    def test_pdf_downloads(self):
        ids = f"{self.a.pk},{self.b.pk}"
        response = self.client.get(reverse("merchant-invoices-combined"), {"ids": ids})
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_no_selection_goes_back_to_the_list(self):
        response = self.client.get(reverse("merchant-invoices-combined"))
        self.assertRedirects(response, reverse("merchant-sales-invoices"))
