"""PPh withheld by the buyer: the invoice is settled, but only the net reaches the bank."""
from decimal import Decimal

from test_sales_docs import Base

from apps.accounting.ledger import PPH_PREPAID, RECEIVABLE, account_balances
from apps.tax.models import Invoice
from apps.tax.services import InvoiceError, record_payment


class BuyerWithholdingTests(Base):
    def balance(self, code):
        return {a.code: a.balance for a in account_balances(self.store)}.get(code, Decimal(0))

    def test_withheld_tax_settles_the_invoice_but_not_the_bank(self):
        invoice = self.invoice(kabel=2)
        due = invoice.amount_due
        withheld = (due * Decimal("0.02")).quantize(Decimal(1))
        payment = record_payment(invoice, due - withheld, self.today, "transfer", withheld=withheld)
        invoice.refresh_from_db()
        self.assertEqual(payment.amount, due)
        self.assertEqual(payment.received, due - withheld)
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(self.balance(PPH_PREPAID), withheld)
        self.assertEqual(self.balance(RECEIVABLE), 0)

    def test_cannot_settle_more_than_is_owed(self):
        invoice = self.invoice(kabel=1)
        with self.assertRaises(InvoiceError):
            record_payment(invoice, invoice.amount_due, self.today, "transfer", withheld=1000)

    def test_payment_without_withholding_is_unchanged(self):
        invoice = self.invoice(kabel=1)
        record_payment(invoice, invoice.amount_due, self.today, "transfer")
        self.assertEqual(self.balance(PPH_PREPAID), 0)


class AnnualCreditTests(Base):
    def test_buyer_withholding_is_a_credit_on_the_annual_worksheet(self):
        from apps.tax import fiscal
        invoice = self.invoice(kabel=2)
        record_payment(invoice, invoice.amount_due - 4_000, self.today, "transfer", withheld=4_000)
        sheet = fiscal.worksheet(self.store, self.today.year)
        self.assertEqual(sheet["buyer_withheld"], 4_000)
        self.assertEqual(sheet["credits_total"], sheet["credits"] + 4_000)
        self.assertEqual(sheet["pph29"], sheet["tax"] - sheet["credits_total"])


class ReceiptWithholdingTests(Base):
    def balance(self, code):
        return {a.code: a.balance for a in account_balances(self.store)}.get(code, Decimal(0))

    def test_one_transfer_two_invoices_each_with_pph(self):
        from apps.tax import receipts
        a, b = self.invoice(kabel=2), self.invoice(kabel=1)
        cash = (a.amount_due - 4_000) + (b.amount_due - 2_000)
        receipt = receipts.receive_payment(self.store, customer=None, buyer_name=a.buyer_name, date=self.today,
            amount=cash, method="transfer", allocations=[(a, a.amount_due - 4_000, 4_000),
            (b, b.amount_due - 2_000, 2_000)], keep_rest=False)
        for invoice in (a, b):
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(receipt.deposit, 0)
        self.assertEqual(self.balance(PPH_PREPAID), 6_000)
        self.assertEqual(self.balance(RECEIVABLE), 0)
        receipts.void_receipt(receipt)
        self.assertEqual(self.balance(PPH_PREPAID), 0)

    def test_receipt_page_takes_pph_per_invoice(self):
        from django.urls import reverse
        invoice = self.invoice(kabel=2)
        self.client.force_login(self.owner)
        due = int(invoice.amount_due)
        url = reverse("merchant-receive-payment")
        response = self.client.post(url, {"customer": self.customer.pk,
            "amount": str(due - 4_000), "date": self.today.isoformat(), "method": "transfer",
            f"alloc_{invoice.pk}": str(due - 4_000), f"pph_{invoice.pk}": "4000"})
        self.assertEqual(response.status_code, 302, [str(m) for m in response.context["messages"]] if response.context else "")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(invoice.payments.get().withheld, 4_000)
