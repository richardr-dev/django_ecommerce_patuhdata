"""Receivables aging per customer, and the manual journal for opening balances."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounting.ledger import CASH, EQUITY, RECEIVABLE, account_balances
from apps.accounting.models import JournalEntry, LedgerLock
from apps.orders.models import Order
from apps.shops.models import Customer, Store
from apps.tax.receivables import aging, statement
from apps.tax.services import issue_invoice_for_order, record_payment


def _balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ar-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-ar")
        self.today = timezone.localdate()
        self.n = 0

    def invoice(self, customer=None, name="Budi", total=100000, days_ago=0, due_in=None):
        self.n += 1
        order = Order.objects.create(store=self.store, customer_name=name, phone="-",
            shipping_address="-", subtotal=Decimal(total), total=Decimal(total), channel="manual",
            customer=customer, idempotency_key=f"ar-{self.n}")
        invoice, _ = issue_invoice_for_order(order, issued_on=self.today - timedelta(days=days_ago))
        if due_in is not None:
            invoice.due_date = self.today - timedelta(days=days_ago) + timedelta(days=due_in)
            invoice.save(update_fields=["due_date"])
        return invoice


class AgingTests(Base):
    def test_open_invoices_age_from_their_due_date(self):
        sari = Customer.objects.create(store=self.store, name="Sari")
        self.invoice(customer=sari, total=100000, days_ago=10, due_in=30)   # not due yet
        self.invoice(customer=sari, total=50000, days_ago=50, due_in=30)    # 20 days late
        self.invoice(customer=sari, total=20000, days_ago=200, due_in=30)   # 170 days late
        self.invoice(name="Tanpa CRM", total=70000, days_ago=0)

        rows, totals = aging(self.store, self.today)

        by_name = {row["name"]: row for row in rows}
        sari_row = by_name["Sari"]
        self.assertEqual((sari_row["current"], sari_row["d30"], sari_row["over"], sari_row["total"]),
            (Decimal(100000), Decimal(50000), Decimal(20000), Decimal(170000)))
        self.assertEqual(by_name["Tanpa CRM"]["current"], Decimal(70000))
        self.assertEqual(totals["total"], Decimal(240000))
        self.assertEqual(totals["total"], _balance(self.store, RECEIVABLE))

    def test_paid_invoices_leave_and_partial_ones_show_the_rest(self):
        paid = self.invoice(total=100000)
        part = self.invoice(total=100000)
        record_payment(paid, Decimal(100000), self.today, "transfer")
        record_payment(part, Decimal(30000), self.today, "transfer")

        _, totals = aging(self.store, self.today)

        self.assertEqual(totals["total"], Decimal(70000))

    def test_statement_and_page(self):
        sari = Customer.objects.create(store=self.store, name="Sari")
        invoice = self.invoice(customer=sari, total=100000, days_ago=40, due_in=30)
        record_payment(invoice, Decimal(25000), self.today, "cash")

        lines, owed = statement(self.store, sari.pk, self.today)
        self.assertEqual((owed, lines[0]["paid"], lines[0]["late_days"]), (Decimal(75000),
            Decimal(25000), 10))

        self.client.force_login(self.owner)
        page = self.client.get(f"/books/sales/piutang/?store={self.store.id}&customer={sari.pk}")
        self.assertContains(page, "Tagihan Sari")
        self.assertContains(page, invoice.number)


class JournalPageTests(Base):
    def post(self, **extra):
        data = {"date": "2026-11-01", "memo": "Saldo awal", "account[]": [CASH, EQUITY, ""],
            "debit[]": ["5.000.000", "", ""], "credit[]": ["", "5.000.000", ""], **extra}
        self.client.force_login(self.owner)
        return self.client.post(f"/books/accounting/journal/new/?store={self.store.id}", data)

    def test_opening_balance_is_booked(self):
        self.post()

        self.assertEqual(_balance(self.store, CASH), Decimal(5000000))
        self.assertEqual(_balance(self.store, EQUITY), Decimal(5000000))
        entry = JournalEntry.objects.get()
        self.assertEqual((entry.source_type, entry.date), ("journal", date(2026, 11, 1)))

    def test_unbalanced_empty_and_closed_period_are_refused(self):
        self.post(**{"credit[]": ["", "4.000.000", ""]})
        self.post(memo="")
        LedgerLock.objects.create(store=self.store, locked_through=date(2026, 11, 30))
        self.post()

        self.assertFalse(JournalEntry.objects.exists())

    def test_forms_load(self):
        self.client.force_login(self.owner)
        base = f"?store={self.store.id}"
        self.assertContains(self.client.get(f"/books/accounting/journal/new/{base}&opening=1"),
            "Saldo Awal")
        self.assertContains(self.client.get(f"/books/sales/piutang/{base}"), "Piutang Pelanggan")
