"""Uang muka kena PPN: PPN on a down payment is due when the money comes in, and counts once over the sale."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting.controls import customer_credit
from apps.accounting.ledger import CUSTOMER_CREDIT, RECEIVABLE, VAT, account_balances
from apps.tax import receipts, spt
from apps.tax.models import InvoicePayment, TaxProfile
from apps.tax.posting import void_invoice


class AdvanceVatTests(Base):
    def advance(self, amount=1_110_000, taxed=True):
        return receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi", date=self.today,
            amount=Decimal(amount), method="transfer", allocations=[], bank_account=self.main,
            advance_vat=taxed, actor=self.owner)

    def tie(self):
        ledger = {a.code: a.balance for a in account_balances(self.store)}
        return customer_credit(self.store, self.today, ledger).status

    def test_ppn_is_booked_when_the_advance_arrives(self):
        receipt = self.advance()
        self.assertEqual((receipt.deposit, receipt.deposit_vat, receipt.deposit_base),
            (Decimal(1_110_000), Decimal(110_000), Decimal(1_000_000)))
        self.assertEqual(balance(self.store, self.main.account.code), Decimal(1_110_000))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(1_000_000))
        self.assertEqual(balance(self.store, VAT), Decimal(110_000))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(1_110_000))
        self.assertEqual(self.tie(), "ok")

    def test_using_it_on_the_invoice_counts_the_sale_ppn_once(self):
        self.advance()
        invoice = self.invoice(kabel=10)  # 1.000.000 + PPN 110.000
        payment = receipts.apply_credit(invoice, Decimal(1_110_000), self.today, actor=self.owner)
        self.assertEqual(payment.vat_settled, Decimal(110_000))
        self.assertEqual(balance(self.store, VAT), Decimal(110_000))  # not 220.000
        self.assertEqual((balance(self.store, CUSTOMER_CREDIT), balance(self.store, RECEIVABLE)), (0, 0))
        self.assertEqual(self.tie(), "ok")
        sheet = spt.worksheet(self.store, self.today.year, self.today.month)
        self.assertEqual(sheet["out"], Decimal(110_000))
        self.assertEqual([r.number for r in sheet["advances"]], [self.advance_number()])
        self.assertEqual([p.vat_settled for p in sheet["advances_used"]], [Decimal(110_000)])

    def advance_number(self):
        from apps.tax.models import CustomerReceipt
        return CustomerReceipt.objects.get(store=self.store).number

    def test_part_use_takes_ppn_in_proportion_and_a_refund_takes_the_rest(self):
        self.advance()
        invoice = self.invoice(kabel=5)  # 555.000
        receipts.apply_credit(invoice, Decimal(555_000), self.today, actor=self.owner)
        self.assertEqual(receipts.credit_vat_balance(self.store, self.customer, "Budi"), Decimal(55_000))
        receipts.refund_credit(self.store, self.customer, "Budi", Decimal(555_000), self.today,
            bank_account=self.main, actor=self.owner)
        self.assertEqual(receipts.credit_vat_balance(self.store, self.customer, "Budi"), 0)
        self.assertEqual(balance(self.store, VAT), Decimal(55_000))  # only the invoice's PPN is left
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), 0)
        self.assertEqual(self.tie(), "ok")

    def test_voiding_the_invoice_gives_the_taxed_credit_back(self):
        self.advance()
        invoice = self.invoice(kabel=10)
        receipts.apply_credit(invoice, Decimal(1_110_000), self.today, actor=self.owner)
        void_invoice(invoice)
        self.assertEqual(receipts.credit_vat_balance(self.store, self.customer, "Budi"), Decimal(110_000))
        self.assertEqual(balance(self.store, VAT), Decimal(110_000))
        self.assertEqual(self.tie(), "ok")

    def test_untaxed_advance_and_non_pkp(self):
        self.advance(taxed=False)
        self.assertEqual((balance(self.store, VAT), balance(self.store, CUSTOMER_CREDIT)), (0, Decimal(1_110_000)))
        TaxProfile.objects.filter(store=self.store).update(is_pkp=False)
        with self.assertRaisesMessage(receipts.ReceiptError, "hanya untuk PKP"):
            self.advance()

    def test_form_offers_it_to_a_pkp_and_books_it(self):
        self.client.force_login(self.owner)
        url = reverse("merchant-receive-payment") + f"?customer={self.customer.id}"
        self.assertContains(self.client.get(url), "faktur uang muka")
        response = self.client.post(url, {"customer": str(self.customer.id), "amount": "2.220.000",
            "date": self.today.isoformat(), "method": "transfer", "bank_account": str(self.main.pk),
            "keep_rest": "1", "advance_vat": "1"}, follow=True)
        self.assertContains(response, "PPN uang muka")
        self.assertEqual(balance(self.store, VAT), Decimal(220_000))
        self.assertFalse(InvoicePayment.objects.exists())
