"""The invoice page shows its journal and says whether the books agree with the amount owed."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.tax import credit_notes
from apps.tax.posting import void_invoice
from apps.tax.services import record_payment


class InvoiceLedgerTieTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.inv = self.invoice(kabel=2, credit=True)

    def page(self):
        return self.client.get(reverse("merchant-invoice-detail", args=[self.inv.pk]))

    def test_open_invoice_ties_and_entries_link_to_the_journal(self):
        page = self.page()
        self.assertEqual(page.context["ledger_tie"]["books"], self.inv.total)
        self.assertContains(page, 'data-ledger-tie="ok"')
        self.assertContains(page, "Piutang usaha di buku besar")
        self.assertContains(page, "/books/accounting/journal/")

    def test_part_payment_and_credit_note_still_tie(self):
        record_payment(self.inv, Decimal(50_000), datetime.date.today(), "transfer")
        self.assertTrue(self.page().context["ledger_tie"]["ok"])
        credit_notes.create_credit_note(self.inv, lines=self.lines(self.inv), issued_on=self.today)
        self.assertTrue(self.page().context["ledger_tie"]["ok"])

    def test_voided_invoice_ties_at_zero(self):
        void_invoice(self.inv)
        tie = self.page().context["ledger_tie"]
        self.assertTrue(tie["ok"])
        self.assertEqual(tie["books"], 0)


class BillLedgerTieTests(Base):
    """The bill page: Utang usaha in the books against what is still owed."""

    def setUp(self):
        super().setUp()
        from datetime import timedelta

        from apps.accounting.models import Supplier
        from apps.accounting.payables import create_bill, receive_bill
        self.client.force_login(self.owner)
        supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        self.bill = create_bill(self.store, supplier=supplier, bill_date=self.today, actor=self.owner,
            due_date=self.today + timedelta(days=14), tax=Decimal(110_000),
            lines=[{"description": "Kabel", "qty": Decimal(10), "unit_price": Decimal(100_000), "sku": "KBL"}])
        receive_bill(self.bill, actor=self.owner)

    def tie(self):
        page = self.client.get(reverse("merchant-bill-detail", args=[self.bill.pk]))
        return page, page.context["ledger_tie"]

    def test_open_part_paid_and_returned_bill_all_tie(self):
        from apps.accounting import supplier_notes
        from apps.accounting.payables import pay_bill
        page, tie = self.tie()
        self.assertTrue(tie["ok"])
        self.assertEqual(tie["books"], self.bill.total)
        self.assertContains(page, "Utang usaha di buku besar")
        pay_bill(self.bill, amount=Decimal(300_000), paid_on=self.today, actor=self.owner)
        self.assertTrue(self.tie()[1]["ok"])
        item = self.bill.items.get()
        supplier_notes.create_debit_note(self.bill, quantities={item.pk: 2}, reason="rusak",
            note_date=self.today, actor=self.owner)
        self.assertTrue(self.tie()[1]["ok"])


class InvoiceOpenBalanceControlTests(Base):
    def check(self):
        from apps.accounting.controls import run_checks
        return next(c for c in run_checks(self.store, self.today) if c.key == "invoice_open_balance")

    def test_ok_when_payment_and_write_off_are_booked(self):
        from apps.tax.services import record_payment
        from apps.tax.writeoff import write_off_invoice
        a = self.invoice(kabel=2)
        b = self.invoice(kabel=1)
        record_payment(a, 10_000, self.today, "transfer")
        write_off_invoice(b, reason="Tidak bayar")
        self.assertEqual(self.check().status, "ok")

    def test_names_an_invoice_paid_in_the_document_but_not_in_the_books(self):
        from apps.tax.models import InvoicePayment
        invoice = self.invoice(kabel=2)
        InvoicePayment.objects.create(invoice=invoice, paid_on=self.today, amount=10_000, method="transfer")
        found = self.check()
        self.assertEqual(found.status, "warn")
        self.assertTrue(any(invoice.number in item for item in found.items))


class CreditNoteAndCashTieTests(Base):
    def test_credit_note_page_shows_the_books_check(self):
        from django.urls import reverse

        from apps.tax import credit_notes
        invoice = self.invoice(kabel=2)
        note = credit_notes.create_credit_note(invoice, issued_on=self.today, actor=self.owner,
            lines=self.lines(invoice))
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-credit-note-detail", args=[note.pk]))
        self.assertContains(page, 'data-ledger-tie="ok"')

    def test_cash_page_shows_the_books_check(self):
        from django.urls import reverse

        from apps.accounting import bank
        from apps.accounting.ledger import chart_of_accounts
        chart_of_accounts(self.store)
        cash = bank.bank_accounts(self.store)[0]
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=cash, date=self.today,
            lines=[("3100", "Setoran modal", 500_000)], actor=self.owner)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-cash-detail", args=[txn.pk]))
        self.assertContains(page, 'data-ledger-tie="ok"')
