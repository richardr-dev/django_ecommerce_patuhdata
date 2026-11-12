"""The ledger as the backbone: every document posts once, corrections reverse, closed periods
stay closed, and modules plug in without the ledger knowing them."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase

from apps.accounting import ledger
from apps.accounting.ledger import (
    CASH,
    INPUT_VAT,
    INVENTORY,
    PAYABLE,
    RECEIVABLE,
    SALES,
    SHIPPING,
    LedgerError,
    LedgerLocked,
    account_balances,
    post_backlog,
    post_entry,
    post_journal,
    post_pending_entries,
    reverse_entry,
)
from apps.accounting.models import Bill, JournalEntry, JournalLine, LedgerLock, Supplier
from apps.accounting.payables import PayableError, create_bill, pay_bill, void_bill
from apps.orders.models import Order, PaymentTransaction
from apps.shops.models import Store
from apps.tax.models import Invoice, TaxProfile
from apps.tax.posting import repost_invoice, void_invoice
from apps.tax.services import InvoiceError, issue_invoice_for_order, monthly_tax, record_payment

TODAY = date(2026, 11, 12)


def _lines(entry):
    return {line.account.code: line.debit - line.credit
        for line in entry.lines.select_related("account")}


def _balance(store, code):
    return {account.code: account.balance for account in account_balances(store)}.get(
        code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("backbone-owner")
        self.store = Store.objects.create(owner=self.owner, name="Buku", slug="buku-besar")

    def _order(self, key="o1", status="pending", **values):
        defaults = {"store": self.store, "customer_name": "Budi", "phone": "0812",
            "shipping_address": "Bandung", "subtotal": Decimal(100000),
            "shipping_cost": Decimal(10000), "total": Decimal(110000), "channel": "manual",
            "status": status, "idempotency_key": key}
        return Order.objects.create(**{**defaults, **values})

    def assert_balanced(self):
        totals = JournalLine.objects.filter(entry__store=self.store).aggregate(
            debit=Sum("debit", default=0), credit=Sum("credit", default=0))
        self.assertEqual(totals["debit"], totals["credit"])


class PostingApiTests(Base):
    def test_a_document_posts_once(self):
        first = post_entry(self.store, date=TODAY, memo="POS", source=("pos_sale", 7),
            amounts={CASH: 50000, SALES: -50000})
        again = post_entry(self.store, date=TODAY, memo="POS", source=("pos_sale", 7),
            amounts={CASH: 50000, SALES: -50000})

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_unbalanced_and_unknown_accounts_are_refused(self):
        with self.assertRaises(LedgerError):
            post_entry(self.store, date=TODAY, memo="x", source=("t", 1), amounts={CASH: 5})
        with self.assertRaises(LedgerError):
            post_entry(self.store, date=TODAY, memo="x", source=("t", 2),
                amounts={CASH: 5, "9999": -5})

    def test_reversal_cancels_and_frees_the_document(self):
        entry = post_entry(self.store, date=TODAY, memo="POS", source=("pos_sale", 7),
            amounts={CASH: 50000, SALES: -50000})

        reversal = reverse_entry(entry)

        self.assertEqual(_lines(reversal), {CASH: Decimal(-50000), SALES: Decimal(50000)})
        self.assertEqual(reverse_entry(entry).pk, reversal.pk)
        self.assertEqual(_balance(self.store, CASH), 0)
        reposted = post_entry(self.store, date=TODAY, memo="POS", source=("pos_sale", 7),
            amounts={CASH: 40000, SALES: -40000})
        self.assertNotEqual(reposted.pk, entry.pk)
        self.assertEqual(_balance(self.store, CASH), Decimal(40000))

    def test_closed_period_refuses_strict_and_moves_automatic_postings(self):
        LedgerLock.objects.create(store=self.store, locked_through=date(2026, 10, 31))

        with self.assertRaises(LedgerLocked):
            post_entry(self.store, date=date(2026, 10, 20), memo="x", source=("t", 1),
                strict=True, amounts={CASH: 5, SALES: -5})
        entry = post_entry(self.store, date=date(2026, 10, 20), memo="x", source=("t", 1),
            amounts={CASH: 5, SALES: -5})
        self.assertEqual(entry.date, date(2026, 11, 1))

    def test_manual_journal_books_owner_capital(self):
        post_journal(self.store, date=TODAY, memo="Setoran modal",
            lines=[(CASH, 1000000, 0), (ledger.EQUITY, 0, 1000000)])

        self.assertEqual(_balance(self.store, ledger.EQUITY), Decimal(1000000))

    def test_modules_register_their_backlog(self):
        calls = []
        poster = ledger.register_backlog(lambda: calls.append(1) or 3)
        self.addCleanup(ledger._BACKLOG.remove, poster)

        self.assertGreaterEqual(post_backlog(), 3)
        self.assertEqual(calls, [1])


class InvoiceLedgerTests(Base):
    def test_credit_invoice_books_receivable_then_payment_clears_it(self):
        invoice, _ = issue_invoice_for_order(self._order(), issued_on=TODAY)

        self.assertEqual(invoice.status, Invoice.STATUS_ISSUED)
        entry = JournalEntry.objects.get(source_type="invoice", source_id=str(invoice.pk))
        self.assertEqual(_lines(entry), {RECEIVABLE: Decimal(110000), SALES: Decimal(-100000),
            SHIPPING: Decimal(-10000)})

        record_payment(invoice, Decimal(60000), TODAY, "transfer")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PARTIAL)
        self.assertEqual(_balance(self.store, RECEIVABLE), Decimal(50000))
        with self.assertRaises(InvoiceError):
            record_payment(invoice, Decimal(50001), TODAY, "transfer")
        record_payment(invoice, Decimal(50000), TODAY, "cash")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(110000))
        self.assert_balanced()

    def test_invoice_issued_as_paid_is_settled_at_once(self):
        invoice, _ = issue_invoice_for_order(self._order(status="paid"), issued_on=TODAY)

        self.assertEqual(invoice.amount_due, 0)
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(110000))

    def test_gateway_paid_order_is_booked_by_its_payment_only(self):
        order = self._order(status="paid")
        PaymentTransaction.objects.create(order=order, provider="demo", external_id="p1",
            status="succeeded", amount=order.total, reconciled=True)
        post_pending_entries()

        issue_invoice_for_order(order, issued_on=TODAY)
        post_backlog()

        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(_balance(self.store, SALES), Decimal(100000))

    def test_gateway_payment_after_a_credit_invoice_settles_the_receivable(self):
        order = self._order()
        issue_invoice_for_order(order, issued_on=TODAY)
        PaymentTransaction.objects.create(order=order, provider="demo", external_id="p1",
            status="succeeded", amount=order.total, reconciled=True)

        post_pending_entries()

        self.assertEqual(_balance(self.store, SALES), Decimal(100000))
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)

    def test_edit_reposts_and_void_reverses(self):
        invoice, _ = issue_invoice_for_order(self._order(), issued_on=TODAY)
        invoice.subtotal = invoice.tax_base = Decimal(80000)
        invoice.total = Decimal(90000)
        invoice.save()

        repost_invoice(invoice)
        self.assertEqual(_balance(self.store, SALES), Decimal(80000))
        self.assertEqual(_balance(self.store, RECEIVABLE), Decimal(90000))

        record_payment(invoice, Decimal(90000), TODAY, "transfer")
        void_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_VOID)
        for code in (SALES, RECEIVABLE, CASH, SHIPPING):
            self.assertEqual(_balance(self.store, code), 0)
        self.assertEqual(post_backlog(), 0)
        self.assert_balanced()

    def test_manual_invoices_count_as_turnover_for_tax(self):
        issue_invoice_for_order(self._order(status="paid"), issued_on=TODAY)

        report = monthly_tax(self.store, 2026, 11)

        self.assertEqual(report.turnover, Decimal(110000))

    def test_backlog_posts_invoices_issued_before_ledger_posting(self):
        invoice, _ = issue_invoice_for_order(self._order(), issued_on=TODAY)
        JournalEntry.objects.all().delete()

        self.assertEqual(post_backlog(), 1)
        self.assertEqual(post_backlog(), 0)
        self.assertEqual(_balance(self.store, RECEIVABLE), invoice.total)


class PayableTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kopi")
        self.lines = [{"description": "Biji kopi", "qty": Decimal(2),
            "unit_price": Decimal(50000)}]

    def _bill(self, **values):
        return create_bill(self.store, supplier=self.supplier, lines=self.lines,
            bill_date=TODAY, tax=Decimal(11000), **values)

    def test_bill_books_stock_and_debt_then_payment_clears_it(self):
        bill = self._bill()

        entry = JournalEntry.objects.get(source_type="bill")
        # Not a PKP: the PPN paid is part of the cost of the goods.
        self.assertEqual(_lines(entry), {INVENTORY: Decimal(111000), PAYABLE: Decimal(-111000)})

        pay_bill(bill, amount=Decimal(100000), paid_on=TODAY)
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.STATUS_PARTIAL)
        with self.assertRaises(PayableError):
            pay_bill(bill, amount=Decimal(11001), paid_on=TODAY)
        pay_bill(bill, amount=Decimal(11000), paid_on=TODAY)
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.STATUS_PAID)
        self.assertEqual(_balance(self.store, PAYABLE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(-111000))
        self.assert_balanced()

    def test_pkp_credits_input_vat_and_expense_bills_hit_their_account(self):
        TaxProfile.objects.create(store=self.store, is_pkp=True)

        self._bill(category="6400")

        self.assertEqual(_lines(JournalEntry.objects.get()), {"6400": Decimal(100000),
            INPUT_VAT: Decimal(11000), PAYABLE: Decimal(-111000)})

    def test_void_reverses_unpaid_bills_only(self):
        bill = self._bill()
        void_bill(bill)
        self.assertEqual(_balance(self.store, PAYABLE), 0)
        self.assertEqual(JournalEntry.objects.count(), 2)

        paid = self._bill(number="B-2")
        pay_bill(paid, amount=Decimal(1000), paid_on=TODAY)
        with self.assertRaises(PayableError):
            void_bill(paid)

    def test_closed_period_refuses_a_bill(self):
        LedgerLock.objects.create(store=self.store, locked_through=TODAY + timedelta(days=1))

        with self.assertRaises(PayableError):
            self._bill()
        self.assertEqual(Bill.objects.count(), 0)


class PageTests(Base):
    def test_owner_closes_and_reopens_the_books(self):
        self.client.force_login(self.owner)
        url = "/merchant/accounting/lock/"

        self.client.post(f"{url}?store={self.store.id}", {"locked_through": "2026-10-31"})
        self.assertEqual(ledger.locked_through(self.store), date(2026, 10, 31))
        page = self.client.get(f"/merchant/accounting/?store={self.store.id}")
        self.assertContains(page, "31/10/2026")

        self.client.post(f"{url}?store={self.store.id}", {"locked_through": ""})
        self.assertIsNone(ledger.locked_through(self.store))


class ManualInvoiceVatTests(Base):
    def _post_invoice(self, **extra):
        from apps.catalog.models import Product
        product, _ = Product.objects.get_or_create(store=self.store, sku="KOPI", defaults={
            "name": "Kopi", "slug": "kopi", "price": 100000})
        self.client.force_login(self.owner)
        data = {"channel": "manual", "buyer_name": "Budi", "phone": "0812",
            "address": "Bandung", "product[]": [product.pk], "description[]": [""], "qty[]": ["1"],
            "unit_price[]": ["100000"], "line_discount[]": [""], "discount": "0", "shipping": "0",
            **extra}
        return self.client.post(f"/merchant/sales/invoices/new/?store={self.store.id}", data)

    def test_a_pkp_seller_charges_ppn_and_books_it(self):
        TaxProfile.objects.create(store=self.store, is_pkp=True)
        self.store.tax_rate = Decimal(11)
        self.store.save()

        self._post_invoice()

        invoice = Invoice.objects.get()
        self.assertEqual((invoice.tax_base, invoice.tax, invoice.total), (Decimal(100000),
            Decimal(11000), Decimal(111000)))
        self.assertEqual(_balance(self.store, ledger.VAT), Decimal(11000))
        self.assertEqual(_balance(self.store, SALES), Decimal(100000))
        # Created as paid: the receivable closes at once and the money is in the bank.
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(111000))

    def test_an_invoice_on_credit_stays_owed_until_paid(self):
        self._post_invoice(payment_status="credit", due_date="2026-12-31")

        invoice = Invoice.objects.get()
        self.assertEqual((invoice.status, invoice.due_date), (Invoice.STATUS_ISSUED,
            date(2026, 12, 31)))
        self.assertEqual(invoice.order.status, "pending")
        self.assertEqual(_balance(self.store, RECEIVABLE), Decimal(100000))

        record_payment(invoice, Decimal(100000), TODAY, "transfer")

        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.order.status), (Invoice.STATUS_PAID, "paid"))
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)

    def test_a_non_pkp_seller_charges_none(self):
        self.store.tax_rate = Decimal(11)
        self.store.save()

        self._post_invoice()

        invoice = Invoice.objects.get()
        self.assertEqual((invoice.tax, invoice.total), (Decimal(0), Decimal(100000)))
