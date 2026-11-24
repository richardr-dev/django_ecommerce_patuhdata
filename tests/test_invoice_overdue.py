"""Jatuh tempo: an unpaid balance past its due date is overdue, part paid or not."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from test_sales_docs import Base

from apps.tax.models import Invoice
from apps.tax.posting import mark_overdue
from apps.tax.services import record_payment


class OverdueTests(Base):
    def test_open_and_part_paid_invoices_past_due_become_overdue(self):
        open_late = self.invoice(days_ago=40, due_days=10)
        part_late = self.invoice(days_ago=40, due_days=10)
        record_payment(part_late, Decimal(50_000), self.today - timedelta(days=35), "transfer")
        not_due = self.invoice(days_ago=1, due_days=30)
        part_late.refresh_from_db()
        self.assertEqual(part_late.status, Invoice.STATUS_OVERDUE)  # set by the payment itself
        self.assertEqual(mark_overdue(self.store), 1)
        for invoice, status in ((open_late, Invoice.STATUS_OVERDUE), (part_late, Invoice.STATUS_OVERDUE),
                (not_due, Invoice.STATUS_ISSUED)):
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, status)

    def test_part_payment_on_an_overdue_invoice_keeps_it_overdue(self):
        invoice = self.invoice(days_ago=40, due_days=10)
        mark_overdue(self.store)
        record_payment(invoice, Decimal(50_000), self.today, "transfer")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_OVERDUE)
        record_payment(invoice, invoice.amount_due, self.today, "transfer")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)

    def test_moving_the_due_date_later_takes_it_out_of_overdue(self):
        invoice = self.invoice(days_ago=40, due_days=10)
        record_payment(invoice, Decimal(50_000), self.today - timedelta(days=35), "transfer")
        mark_overdue(self.store)
        Invoice.objects.filter(pk=invoice.pk).update(due_date=self.today + timedelta(days=7))
        mark_overdue(self.store)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PARTIAL)

    def test_books_worker_marks_overdue(self):
        invoice = self.invoice(days_ago=40, due_days=10)
        call_command("run_books", stdout=StringIO())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_OVERDUE)
