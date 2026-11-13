"""Anggaran: plan by account and month, compare with the ledger, per cost centre, with alerts."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import budgets, controls
from apps.accounting.expenses import ExpenseForm
from apps.accounting.ledger import post_journal
from apps.accounting.models import Account, BudgetLine, CostCenter, Expense, Supplier
from apps.accounting.payables import create_bill


class BudgetTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.today = datetime.date.today()
        from apps.accounting.ledger import chart_of_accounts
        chart_of_accounts(self.store)
        self.rent = Account.objects.get(store=self.store, code="6210") if Account.objects.filter(store=self.store, code="6210").exists() \
            else Account.objects.filter(store=self.store, type="expense").exclude(code__in=["5100", "6100"]).first()
        self.center = CostCenter.objects.create(store=self.store, code="BDG", name="Cabang Bandung")

    def spend(self, amount, center=None):
        expense = Expense.objects.create(store=self.store, date=self.today, category=self.rent.code,
            description="Sewa", amount=amount, created_by=self.owner, cost_center=center)
        from apps.accounting.ledger import post_expense
        post_expense(expense)
        return expense

    def test_save_and_read_a_budget_and_zero_removes_a_line(self):
        rows = {self.rent.code: [Decimal(1_000_000)] * 12}
        self.assertEqual(budgets.save_budget(self.store, self.today.year, rows), 12)
        self.assertEqual(budgets.get_budget(self.store, self.today.year)[self.rent.code][3], Decimal(1_000_000))
        budgets.save_budget(self.store, self.today.year, {self.rent.code: [0] * 12})
        self.assertEqual(BudgetLine.objects.filter(store=self.store).count(), 0)
        with self.assertRaises(budgets.BudgetError):
            budgets.save_budget(self.store, self.today.year, {"1100": [100] * 12})   # cash is not budgetable
        with self.assertRaises(budgets.BudgetError):
            budgets.save_budget(self.store, self.today.year, {self.rent.code: [-5] + [0] * 11})

    def test_actual_comes_from_the_ledger_and_status_follows_the_percentage(self):
        month = self.today.month
        budgets.save_budget(self.store, self.today.year, {self.rent.code: [Decimal(1_000_000)] * 12})
        self.spend(500_000)
        row = budgets.report(self.store, self.today.year, month, month)["expense"][0]
        self.assertEqual((row["actual"], row["pct"], row["status"]), (Decimal(500_000), 50, "ok"))
        self.spend(350_000)
        self.assertEqual(budgets.report(self.store, self.today.year, month, month)["expense"][0]["status"], "warn")
        self.spend(300_000)
        data = budgets.report(self.store, self.today.year, month, month)
        self.assertEqual((data["expense"][0]["status"], data["expense"][0]["variance"]), ("over", Decimal(-150_000)))
        self.assertEqual(len(data["over"]), 1)

    def test_a_range_of_months_adds_the_budget_up(self):
        budgets.save_budget(self.store, self.today.year, {self.rent.code: [Decimal(100)] * 12})
        data = budgets.report(self.store, self.today.year, 1, 3)
        self.assertEqual(data["expense"][0]["budget"], Decimal(300))

    def test_spending_without_a_budget_is_flagged(self):
        self.spend(80_000)
        data = budgets.report(self.store, self.today.year, self.today.month, self.today.month)
        self.assertTrue(data["expense"][0]["unbudgeted"])
        self.assertEqual(data["expense"][0]["status"], "none")

    def test_cost_centres_keep_their_own_actuals_and_budgets(self):
        month = self.today.month
        budgets.save_budget(self.store, self.today.year, {self.rent.code: [Decimal(400_000)] * 12}, self.center)
        self.spend(300_000, self.center)
        self.spend(900_000)                                       # the rest of the business
        bandung = budgets.report(self.store, self.today.year, month, month, self.center)["expense"][0]
        whole = budgets.report(self.store, self.today.year, month, month)["expense"]
        self.assertEqual((bandung["actual"], bandung["pct"]), (Decimal(300_000), 75))
        self.assertEqual(whole[0]["actual"], Decimal(1_200_000))    # business-wide counts everything
        self.assertEqual(whole[0]["budget"], 0)                     # the centre's budget is not the company's

    def test_bills_and_manual_journals_carry_the_cost_centre(self):
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        from apps.accounting.models import BILL_CATEGORIES
        category = next(code for code, _ in BILL_CATEGORIES if code not in {"1400", "1600"})
        self.rent = Account.objects.get(store=self.store, code=category)
        bill = create_bill(self.store, supplier=supplier, lines=[{"description": "x", "qty": 1,
            "unit_price": Decimal(200_000)}], bill_date=self.today, category=category,
            cost_center=self.center)
        from apps.accounting.models import JournalEntry
        self.assertEqual(JournalEntry.objects.get(source_type="bill", source_id=str(bill.pk)).cost_center, self.center)
        entry = post_journal(self.store, date=self.today, memo="x", lines=[(self.rent.code, 50_000, 0),
            ("1100", 0, 50_000)], cost_center=self.center)
        self.assertEqual(entry.cost_center, self.center)
        self.assertEqual(budgets.actuals(self.store, self.today.replace(day=1), self.today, self.center)[self.rent.code],
            Decimal(250_000))

    def test_expense_form_offers_only_this_stores_active_centres(self):
        CostCenter.objects.create(store=self.store, code="OLD", name="Lama", is_active=False)
        form = ExpenseForm(store=self.store)
        self.assertEqual(list(form.fields["cost_center"].queryset), [self.center])

    def test_pages_editor_and_centres(self):
        url = reverse("merchant-budget-edit")
        post = {"year": self.today.year}
        post.update({f"b_{self.rent.code}_{m}": "1.000.000" for m in range(1, 13)})
        self.assertEqual(self.client.post(url, post).status_code, 302)
        self.assertEqual(budgets.get_budget(self.store, self.today.year)[self.rent.code][0], Decimal(1_000_000))
        self.spend(900_000)
        page = self.client.get(reverse("merchant-budget"))
        self.assertContains(page, "Anggaran vs realisasi")
        self.assertContains(page, "90%")
        self.assertContains(self.client.get(url), "Atur anggaran")
        self.client.post(reverse("merchant-cost-centers"), {"action": "add", "code": "jkt", "name": "Cabang Jakarta"})
        self.assertTrue(CostCenter.objects.filter(store=self.store, code="JKT").exists())
        self.client.post(reverse("merchant-cost-centers"), {"action": "add", "code": "JKT", "name": "Lagi"})
        self.assertEqual(CostCenter.objects.filter(store=self.store, code="JKT").count(), 1)

    def test_viewers_cannot_edit_the_budget(self):
        from django.contrib.auth.models import User

        from apps.shops.models import StoreStaff
        viewer = User.objects.create_user("vw2", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-budget")).status_code, 200)
        self.assertIn(self.client.get(reverse("merchant-budget-edit")).status_code, (403, 404))

    def test_over_budget_shows_in_beranda_and_the_books_check(self):
        budgets.save_budget(self.store, self.today.year, {self.rent.code: [Decimal(100_000)] * 12})
        self.spend(250_000)
        from apps.accounting.todo import todo_items
        labels = [i["label"] for i in todo_items(self.store, self.today)]
        self.assertIn("pos biaya melebihi anggaran bulan ini", labels)
        check = next(c for c in controls.run_checks(self.store, self.today) if c.key == "budget")
        self.assertEqual(check.status, controls.WARN)
