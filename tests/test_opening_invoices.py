"""Saldo awal piutang per invoice: old unpaid invoices become real invoices that book no sale."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import opening
from apps.accounting.controls import receivable
from apps.accounting.ledger import EQUITY, RECEIVABLE, SALES, VAT, account_balances, live_entry
from apps.channels.invoice_recap import invoice_recap
from apps.channels.reporting import sales_report
from apps.orders.scope import website_orders
from apps.shops.models import Customer
from apps.tax import credit_notes
from apps.tax import opening_invoices as service
from apps.tax.models import Invoice
from apps.tax.posting import void_invoice
from apps.tax.sales import invoice_locks
from apps.tax.services import record_payment


class OpeningInvoiceTests(Base):
    def carry(self, amount=2_500_000, number="LAMA-001", days_ago=40, due_days=30, **kw):
        issued = self.today - timedelta(days=days_ago)
        return service.create_opening_invoice(self.store, customer=self.customer, number=number,
            issued_on=issued, due_date=issued + timedelta(days=due_days), amount=Decimal(amount),
            actor=self.owner, **kw)

    def ledger(self):
        return {a.code: a.balance for a in account_balances(self.store)}

    def test_books_only_receivable_against_owner_equity(self):
        invoice = self.carry()
        entry = live_entry(self.store, "invoice", invoice.pk)
        lines = {line.account.code: (line.debit, line.credit) for line in entry.lines.select_related("account")}
        self.assertEqual(lines, {RECEIVABLE: (Decimal(2_500_000), 0), EQUITY: (0, Decimal(2_500_000))})
        self.assertEqual((balance(self.store, SALES), balance(self.store, VAT)), (0, 0))
        self.assertEqual((invoice.number, invoice.tax, invoice.status), ("LAMA-001", 0, Invoice.STATUS_OVERDUE))
        self.assertIn("saldo awal dari pembukuan lama", invoice_locks(invoice))

    def test_posts_on_the_opening_date_and_refuses_a_later_invoice(self):
        start = self.today - timedelta(days=10)
        opening.post_opening(self.store, date=start, banks={self.main: Decimal(1_000_000)})
        invoice = self.carry()
        self.assertEqual(live_entry(self.store, "invoice", invoice.pk).date, start)
        with self.assertRaisesMessage(service.OpeningInvoiceError, "penjualan baru"):
            self.carry(number="LAMA-002", days_ago=2)

    def test_paid_written_off_and_tied_like_any_invoice(self):
        invoice = self.carry()
        record_payment(invoice, Decimal(1_000_000), self.today, "transfer", bank_account=self.main)
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.amount_due), (Invoice.STATUS_OVERDUE, Decimal(1_500_000)))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(1_500_000))
        self.assertEqual(receivable(self.store, self.today, self.ledger()).status, "ok")

    def test_not_a_sale_anywhere(self):
        self.invoice(kabel=1, days_ago=1)
        start, end = self.today - timedelta(days=90), self.today
        recap = invoice_recap(self.store, start, end, today=self.today)["totals"]
        report = sales_report(self.store, start, end)
        orders = website_orders(self.store).count()
        invoice = self.carry()
        record_payment(invoice, invoice.total, self.today, "transfer", bank_account=self.main)
        self.assertEqual(invoice_recap(self.store, start, end, today=self.today)["totals"], recap)
        self.assertEqual(sales_report(self.store, start, end), report)
        self.assertEqual(website_orders(self.store).count(), orders)

    def test_no_credit_note_and_void_reverses(self):
        invoice = self.carry()
        with self.assertRaisesMessage(credit_notes.CreditNoteError, "saldo awal"):
            credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        void_invoice(invoice)
        self.assertEqual(balance(self.store, RECEIVABLE), 0)

    def test_number_already_used_is_refused(self):
        self.carry()
        with self.assertRaisesMessage(service.OpeningInvoiceError, "sudah dipakai"):
            self.carry()

    def test_paste_creates_customers_and_is_all_or_nothing(self):
        text = ("Pelanggan\tNo\tTanggal\tJatuh tempo\tSisa\n"
                "Budi\tA-1\t01/09/2026\t01/10/2026\t1.250.000\n"
                "CV Baru;A-2;2026-09-05;;750000\n")
        created = service.import_rows(self.store, service.parse(text), actor=self.owner)
        self.assertEqual([i.total for i in created], [Decimal(1_250_000), Decimal(750_000)])
        self.assertEqual(created[0].order.customer, self.customer)
        self.assertTrue(Customer.objects.filter(store=self.store, name="CV Baru").exists())
        bad = "Toko X;A-3;01/09/2026;;100000\nToko Y;A-4;kemarin;;100000"
        with self.assertRaisesMessage(service.OpeningInvoiceError, "Baris 2"):
            service.import_rows(self.store, service.parse(bad), actor=self.owner)
        self.assertFalse(Invoice.objects.filter(number="A-3").exists())

    def test_page_adds_one_and_lists_it(self):
        self.client.force_login(self.owner)
        url = reverse("merchant-opening-invoices")
        response = self.client.post(url, {"customer": "Budi", "number": "OLD-7", "amount": "300.000",
            "issued_on": (self.today - timedelta(days=60)).isoformat(), "idem": "k1"}, follow=True)
        self.assertContains(response, "OLD-7")
        self.assertEqual(response.context["owed"], Decimal(300_000))
        invoice = Invoice.objects.get(store=self.store, number="OLD-7")
        detail = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]))
        self.assertContains(detail, "Saldo awal dari pembukuan lama")
        self.assertNotContains(detail, "Buat nota kredit")
        self.assertContains(self.client.get(reverse("merchant-opening-balances")), url)
