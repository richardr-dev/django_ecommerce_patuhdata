"""Shared UI behaviour: htmx partials, global search, finance to-do, bank account on bill
payments and expenses, and the books worker."""
import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting import assets, bank
from apps.accounting.ledger import CASH, account_balances
from apps.accounting.models import Bill, Expense, Supplier
from apps.accounting.payables import create_bill, pay_bill
from apps.accounting.todo import todo_items
from apps.orders.search import search
from apps.shops.models import Customer, Store
from apps.tax.models import TaxProfile


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ui-owner", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Toko UI", slug="toko-ui",
            tax_rate=Decimal(11))
        TaxProfile.objects.create(store=self.store, is_pkp=True)
        self.today = timezone.localdate()
        self.main = bank.bank_accounts(self.store)[0]
        self.client.force_login(self.owner)


class PartialTests(Base):
    def setUp(self):
        super().setUp()
        for name in ("CV Maju", "PT Lestari"):
            supplier = Supplier.objects.create(store=self.store, name=name)
            create_bill(self.store, supplier=supplier, bill_date=self.today, number=f"F-{name[:2]}",
                lines=[{"description": "Jasa", "qty": 1, "unit_price": 100_000}])

    def test_full_page_and_htmx_partial(self):
        full = self.client.get(reverse("merchant-bills"))
        self.assertContains(full, "<html")
        partial = self.client.get(reverse("merchant-bills") + "?q=Maju", HTTP_HX_REQUEST="true")
        self.assertNotContains(partial, "<html")
        self.assertContains(partial, "CV Maju")
        self.assertNotContains(partial, "PT Lestari")
        self.assertIn("HX-Request", partial["Vary"])

    def test_history_restore_gets_the_whole_page(self):
        response = self.client.get(reverse("merchant-bills"), HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true")
        self.assertContains(response, "<html")

    def test_status_filter_and_suppliers_search(self):
        page = self.client.get(reverse("merchant-bills") + "?status=paid")
        self.assertContains(page, "Tidak ada tagihan yang cocok")
        found = self.client.get(reverse("merchant-suppliers") + "?q=lest", HTTP_HX_REQUEST="true")
        self.assertContains(found, "PT Lestari")
        self.assertNotContains(found, "CV Maju")

    def test_bills_tiles_show_overdue(self):
        supplier = Supplier.objects.get(name="CV Maju")
        create_bill(self.store, supplier=supplier, bill_date=self.today - datetime.timedelta(days=40),
            due_date=self.today - datetime.timedelta(days=10),
            lines=[{"description": "Lama", "qty": 1, "unit_price": 50_000}])
        page = self.client.get(reverse("merchant-bills"))
        self.assertContains(page, "Lewat jatuh tempo")
        self.assertContains(page, "50.000")


class SearchTests(Base):
    def test_finds_across_records_and_ignores_short_or_other_store_queries(self):
        Customer.objects.create(store=self.store, name="Budi Santoso", phone="0812")
        supplier = Supplier.objects.create(store=self.store, name="Budi Supplier")
        create_bill(self.store, supplier=supplier, bill_date=self.today, number="BUDI-1",
            lines=[{"description": "x", "qty": 1, "unit_price": 1000}])
        titles = [group["title"] for group in search(self.store, "budi")]
        self.assertIn("Pelanggan", titles)
        self.assertIn("Supplier", titles)
        self.assertIn("Tagihan supplier", titles)
        self.assertEqual(search(self.store, "b"), [])
        other = Store.objects.create(owner=User.objects.create_user("o2"), name="Lain", slug="lain")
        self.assertEqual(search(other, "budi"), [])

    def test_endpoint_returns_links(self):
        Customer.objects.create(store=self.store, name="Sari Dewi")
        page = self.client.get(reverse("merchant-search") + "?q=sari")
        self.assertContains(page, "Sari Dewi")
        self.assertContains(page, "Pelanggan")
        self.assertEqual(self.client.get(reverse("merchant-search") + "?q=s").content.strip(), b"")


class BankAccountOnPaymentsTests(Base):
    def test_bill_payment_leaves_the_chosen_account(self):
        petty = bank.add_bank_account(self.store, name="BCA")
        supplier = Supplier.objects.create(store=self.store, name="CV X")
        bill = create_bill(self.store, supplier=supplier, bill_date=self.today,
            lines=[{"description": "x", "qty": 1, "unit_price": 100_000}])
        pay_bill(bill, amount=Decimal(60_000), paid_on=self.today, bank_account=petty)
        self.assertEqual(balance(self.store, petty.account.code), Decimal(-60_000))
        self.assertEqual(balance(self.store, CASH), Decimal(0))

    def test_bill_page_pays_from_a_bank_account(self):
        petty = bank.add_bank_account(self.store, name="Mandiri")
        supplier = Supplier.objects.create(store=self.store, name="CV Y")
        bill = create_bill(self.store, supplier=supplier, bill_date=self.today,
            lines=[{"description": "x", "qty": 1, "unit_price": 100_000}])
        self.client.post(reverse("merchant-bill-detail", args=[bill.id]), {"action": "pay",
            "amount": "100000", "paid_on": self.today.isoformat(), "method": "transfer",
            "bank_account": petty.id})
        self.assertEqual(balance(self.store, petty.account.code), Decimal(-100_000))
        self.assertEqual(Bill.objects.get(pk=bill.pk).status, "paid")

    def test_expense_form_posts_to_the_chosen_account(self):
        petty = bank.add_bank_account(self.store, name="Kas kecil", kind="cash")
        page = self.client.get(reverse("merchant-expenses"))
        self.assertContains(page, "Dibayar dari rekening")
        response = self.client.post(reverse("merchant-expenses"), {"date": self.today.isoformat(),
            "category": "6300", "description": "Sewa", "amount": "500000", "input_vat": "",
            "bank_account": petty.id})
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.bank_account_id, petty.id)
        self.assertEqual(balance(self.store, petty.account.code), Decimal(-500_000))


class TodoAndWorkerTests(Base):
    def test_todo_lists_what_is_waiting(self):
        supplier = Supplier.objects.create(store=self.store, name="CV Z")
        create_bill(self.store, supplier=supplier, bill_date=self.today,
            due_date=self.today + datetime.timedelta(days=3),
            lines=[{"description": "x", "qty": 1, "unit_price": 1000}])
        assets.acquire(self.store, name="Laptop", acquired_on=self.today - datetime.timedelta(days=70),
            cost=12_000_000, useful_life_months=48, paid_from_code=CASH)
        items = {item["url"]: item for item in todo_items(self.store, self.today)}
        self.assertIn(reverse("merchant-payables"), items)
        self.assertIn(reverse("merchant-assets"), items)
        self.assertEqual(todo_items(Store.objects.create(owner=self.owner, name="Kosong",
            slug="kosong"), self.today), [])

    def test_dashboard_shows_the_finance_rows(self):
        supplier = Supplier.objects.create(store=self.store, name="CV Q")
        create_bill(self.store, supplier=supplier, bill_date=self.today,
            due_date=self.today, lines=[{"description": "x", "qty": 1, "unit_price": 1000}])
        page = self.client.get(reverse("merchant-dashboard"))
        self.assertContains(page, "tagihan supplier jatuh tempo")

    def test_worker_books_depreciation_and_is_repeatable(self):
        assets.acquire(self.store, name="Laptop", acquired_on=self.today - datetime.timedelta(days=70),
            cost=4_800_000, useful_life_months=48, paid_from_code=CASH)
        out = StringIO()
        call_command("run_books", stdout=out)
        first = balance(self.store, "6950")
        self.assertGreater(first, 0)
        call_command("run_books", stdout=out)
        self.assertEqual(balance(self.store, "6950"), first)


class ConfirmMarkupTests(Base):
    def test_destructive_actions_use_the_styled_confirm(self):
        page = self.client.get(reverse("merchant-dashboard"))
        self.assertContains(page, 'id="pd-confirm"')
        self.assertContains(page, "merchant-ui.js")

    def test_trial_balance_csv(self):
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[("3100", "Modal", 1_000_000)])
        response = self.client.get(reverse("merchant-trial-balance") + "?format=csv")
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("Modal pemilik", response.content.decode())

    def test_rebuilt_pages_render(self):
        for name in ("merchant-trial-balance", "merchant-chart-of-accounts", "merchant-journal-new",
                "merchant-receivables", "merchant-suppliers", "merchant-bills", "merchant-expenses",
                "merchant-sales-invoices"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
