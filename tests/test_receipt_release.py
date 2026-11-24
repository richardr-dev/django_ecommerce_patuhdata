"""Lepas ke kredit pelanggan: money put on the wrong invoice becomes the buyer's credit, the bank stays."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting.ledger import CUSTOMER_CREDIT, RECEIVABLE
from apps.shops.models import AuditLog
from apps.tax import receipts
from apps.tax.models import Invoice


class ReleaseTests(Base):
    def receive(self, invoice, amount, withheld=0):
        return receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi", date=self.today,
            amount=amount, method="transfer", allocations=[(invoice, amount, withheld)],
            bank_account=self.main, actor=self.owner)

    def test_wrong_invoice_payment_becomes_credit_and_the_bank_is_untouched(self):
        wrong, right = self.invoice(kabel=1), self.invoice(kabel=1)
        receipt = self.receive(wrong, Decimal(111_000))
        bank_before = balance(self.store, self.main.account.code)

        released = receipts.release_to_credit(receipt, actor=self.owner)

        wrong.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual(released, Decimal(111_000))
        self.assertEqual(wrong.status, Invoice.STATUS_ISSUED)
        self.assertEqual((receipt.deposit, receipt.allocations.count()), (Decimal(111_000), 0))
        self.assertEqual(balance(self.store, self.main.account.code), bank_before)
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(222_000))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(111_000))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(111_000))

        receipts.apply_credit(right, Decimal(111_000), self.today, actor=self.owner)  # now the right one
        right.refresh_from_db()
        self.assertEqual(right.status, Invoice.STATUS_PAID)
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), 0)

    def test_part_deposit_receipts_keep_their_earlier_credit(self):
        invoice = self.invoice(kabel=1)
        receipt = receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi", date=self.today,
            amount=Decimal(150_000), method="transfer", allocations=[(invoice, Decimal(111_000))],
            bank_account=self.main, actor=self.owner)
        receipts.release_to_credit(receipt, actor=self.owner)
        receipt.refresh_from_db()
        self.assertEqual(receipt.deposit, Decimal(150_000))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(150_000))

    def test_withheld_pph_is_refused(self):
        invoice = self.invoice(kabel=1)
        receipt = self.receive(invoice, Decimal(109_000), withheld=Decimal(2_000))
        with self.assertRaisesMessage(receipts.ReceiptError, "PPh yang dipotong"):
            receipts.release_to_credit(receipt, actor=self.owner)

    def test_page_button_releases_and_logs(self):
        receipt = self.receive(self.invoice(kabel=1), Decimal(111_000))
        self.client.force_login(self.owner)
        url = reverse("merchant-receipt-detail", args=[receipt.pk])
        self.assertContains(self.client.get(url), "Lepas ke kredit pelanggan")
        self.client.post(url, {"action": "release"})
        self.assertTrue(AuditLog.objects.filter(action="receipt.released_to_credit",
            object_id=str(receipt.pk)).exists())
        self.assertNotContains(self.client.get(url), "Lepas ke kredit pelanggan")
