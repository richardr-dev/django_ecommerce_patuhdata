"""Writing off an unpaid invoice: provision first, then expense; the invoice is closed and reversible."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.ledger import (
    ALLOWANCE,
    BAD_DEBT,
    RECEIVABLE,
    account_balances,
    post_entry,
    reverse_source,
)
from apps.tax.models import Invoice
from apps.tax.services import InvoiceError, record_payment
from apps.tax.writeoff import write_off_invoice


class WriteOffTests(Base):
    def balance(self, code):
        return {a.code: a.balance for a in account_balances(self.store)}.get(code, Decimal(0))

    def test_without_provision_it_all_goes_to_expense(self):
        invoice = self.invoice(kabel=2)
        due = invoice.amount_due
        write_off_invoice(invoice, reason="Pembeli hilang", actor=self.owner)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(self.balance(RECEIVABLE), 0)
        self.assertEqual(self.balance(BAD_DEBT), due)

    def test_provision_is_used_first(self):
        invoice = self.invoice(kabel=2)
        due = invoice.amount_due
        provision = due / 2
        post_entry(self.store, date=self.today, memo="Cadangan", amounts={BAD_DEBT: provision, ALLOWANCE: -provision},
            source=("test", "provision"))
        write_off_invoice(invoice, reason="Tidak bayar", actor=self.owner)
        self.assertEqual(self.balance(ALLOWANCE), 0)
        self.assertEqual(self.balance(BAD_DEBT), due)  # provision expense already booked + the rest
        self.assertEqual(self.balance(RECEIVABLE), 0)

    def test_part_paid_writes_off_only_the_rest(self):
        invoice = self.invoice(kabel=2)
        record_payment(invoice, 10_000, self.today, "transfer")
        invoice.refresh_from_db()
        rest = invoice.amount_due
        payment = write_off_invoice(invoice, reason="Sisa tidak dibayar")
        self.assertEqual(payment.amount, rest)

    def test_reason_required_and_paid_invoice_refused(self):
        invoice = self.invoice(kabel=1)
        with self.assertRaises(InvoiceError):
            write_off_invoice(invoice, reason=" ")
        record_payment(invoice, invoice.amount_due, self.today, "transfer")
        invoice.refresh_from_db()
        with self.assertRaises(InvoiceError):
            write_off_invoice(invoice, reason="x")

    def test_voiding_the_write_off_brings_the_receivable_back(self):
        invoice = self.invoice(kabel=1)
        due = invoice.amount_due
        payment = write_off_invoice(invoice, reason="Salah")
        reverse_source(self.store, "invoice_payment", payment.pk)
        self.assertEqual(self.balance(RECEIVABLE), due)

    def test_cannot_use_record_payment_for_it(self):
        invoice = self.invoice(kabel=1)
        with self.assertRaises(InvoiceError):
            record_payment(invoice, 1000, self.today, "writeoff")

    def test_page_action(self):
        invoice = self.invoice(kabel=1)
        self.client.force_login(self.owner)
        url = reverse("merchant-invoice-detail", args=[invoice.id])
        self.assertContains(self.client.get(url), "Hapus piutang")
        self.client.post(url, {"action": "writeoff", "reason": "Pailit", "date": self.today.isoformat()})
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertContains(self.client.get(url), "Piutang dihapus")
