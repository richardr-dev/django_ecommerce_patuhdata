"""Penggajian: salaries, BPJS and PPh 21 posted to the ledger, paid, and tied by the controls.

Worked example (2025 BPJS defaults):
  Ani: gaji pokok 8.000.000 + tunjangan 2.000.000 (BPJS wage 10.000.000) + bonus 500.000, TER 3%.
    BPJS company: Kesehatan 400.000, JHT 370.000, JKK 24.000, JKM 30.000, JP 200.000 = 1.024.000
    BPJS employee: Kesehatan 100.000, JHT 200.000, JP 100.000 = 400.000
    PPh 21 base 10.500.000 + (400.000 + 24.000 + 30.000) = 10.954.000 × 3% = 328.620
    Take home 10.500.000 − 400.000 − 328.620 = 9.771.380
  Budi: gaji pokok 4.000.000, no BPJS, TER 0 → 4.000.000."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import approvals, payroll
from apps.accounting.controls import payroll_tie, withholding_tie
from apps.accounting.ledger import (
    BPJS_EXPENSE,
    BPJS_PAYABLE,
    SALARIES,
    WAGES_PAYABLE,
    WITHHELD,
    account_balances,
    live_entry,
)
from apps.accounting.models import ApprovalPolicy, Employee, PayrollRun, Withholding
from apps.shops.models import StoreStaff

D = Decimal


class PayrollTests(Base):
    def setUp(self):
        super().setUp()
        self.ani = Employee.objects.create(store=self.store, name="Ani", ptkp_status="K/1", base_salary=D(8_000_000),
            allowance=D(2_000_000), pph21_rate=D(3), npwp="1234567890123456")
        self.budi = Employee.objects.create(store=self.store, name="Budi", base_salary=D(4_000_000), bpjs=False)
        self.period = datetime.date(self.today.year, self.today.month, 1)

    def draft(self, bonus=500_000):
        run = payroll.create_run(self.store, self.period, actor=self.owner)
        line = run.lines.get(employee=self.ani)
        payroll.update_lines(run, {line.pk: {"extra": D(bonus)}}, actor=self.owner)
        return PayrollRun.objects.get(pk=run.pk)

    def checks(self):
        ledger = {a.code: a.balance for a in account_balances(self.store)}
        return payroll_tie(self.store, self.today, ledger).status, withholding_tie(self.store, self.today, ledger).status

    def test_worked_example(self):
        run = self.draft()
        ani = run.lines.get(employee=self.ani)
        self.assertEqual((ani.bpjs_company, ani.bpjs_employee, ani.bpjs_taxable),
            (D(1_024_000), D(400_000), D(454_000)))
        self.assertEqual((ani.pph21_base, ani.pph21, ani.net), (D(10_954_000), D(328_620), D(9_771_380)))
        self.assertEqual(run.lines.get(employee=self.budi).net, D(4_000_000))
        self.assertEqual(self.ani.ter_category, "B")

    def test_posting_books_salaries_bpjs_and_pph21(self):
        run = payroll.post_run(self.draft(), actor=self.owner)
        entry = live_entry(self.store, "payroll", run.pk)
        self.assertEqual(entry.date, payroll.default_post_date(self.period, self.today))
        self.assertEqual(payroll.default_post_date(datetime.date(2026, 1, 1), self.today), datetime.date(2026, 1, 31))
        lines = {line.account.code: (line.debit, line.credit) for line in entry.lines.select_related("account")}
        self.assertEqual(lines, {SALARIES: (D(14_500_000), 0), BPJS_EXPENSE: (D(1_024_000), 0),
            BPJS_PAYABLE: (0, D(1_424_000)), WITHHELD: (0, D(328_620)), WAGES_PAYABLE: (0, D(13_771_380))})
        slip = Withholding.objects.get(store=self.store, kind="pph21")
        self.assertEqual((slip.payee_name, slip.amount, slip.base), ("Ani", D(328_620), D(10_954_000)))
        self.assertEqual(self.checks(), ("ok", "ok"))

    def test_pay_wages_and_bpjs(self):
        run = payroll.post_run(self.draft(), actor=self.owner)
        payroll.pay_run(run, date=self.today + datetime.timedelta(days=1), bank_account=self.main, actor=self.owner)
        payroll.pay_bpjs(run, date=self.today + datetime.timedelta(days=2), bank_account=self.main, actor=self.owner)
        self.assertEqual((balance(self.store, WAGES_PAYABLE), balance(self.store, BPJS_PAYABLE)), (0, 0))
        self.assertEqual(balance(self.store, self.main.account.code), -D(13_771_380 + 1_424_000))
        self.assertEqual(self.checks(), ("ok", "ok"))
        with self.assertRaisesMessage(payroll.PayrollError, "sudah dibayar"):
            payroll.void_run(run)

    def test_rate_text(self):
        self.assertEqual([payroll.rate_text(D(x)) for x in ("3.00", "0.25", "10", "1.50")], ["3", "0,25", "10", "1,5"])

    def test_wage_caps(self):
        self.ani.base_salary, self.ani.allowance = D(15_000_000), 0
        self.ani.save()
        line = payroll.create_run(self.store, self.period).lines.get(employee=self.ani)
        parts = payroll.bpjs_parts(D(15_000_000), payroll.settings_for(self.store))
        self.assertEqual(parts["BPJS Kesehatan"], (D(480_000), D(120_000)))  # capped at 12.000.000
        self.assertEqual(parts["JP"], (D(210_948), D(105_474)))  # capped at 10.547.400
        self.assertEqual(line.bpjs_employee, D(120_000 + 300_000 + 105_474))

    def test_void_reverses_and_one_live_run_per_month(self):
        run = payroll.post_run(self.draft(), actor=self.owner)
        with self.assertRaisesMessage(payroll.PayrollError, "sudah ada"):
            payroll.create_run(self.store, self.period)
        payroll.void_run(run, actor=self.owner)
        self.assertEqual((balance(self.store, SALARIES), balance(self.store, WITHHELD)), (0, 0))
        self.assertFalse(Withholding.objects.filter(store=self.store).exists())
        payroll.create_run(self.store, self.period)  # the month can be drafted again
        self.assertEqual(self.checks(), ("ok", "ok"))

    def test_above_threshold_waits_for_another_person(self):
        ApprovalPolicy.objects.create(store=self.store, kind="payroll", threshold=D(1_000_000), enabled=True)
        boss = User.objects.create_user("boss")
        StoreStaff.objects.create(store=self.store, user=boss, role="finance")
        run = self.draft()
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-payroll-run", args=[run.pk]), {"action": "post"})
        run.refresh_from_db()
        self.assertEqual((run.status, run.approval_pending), (PayrollRun.DRAFT, True))
        request = approvals.ApprovalRequest.objects.get(store=self.store, kind="payroll")
        approvals.approve(request, boss)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.POSTED)
        self.assertEqual(live_entry(self.store, "payroll", run.pk).created_by, self.owner)

    def test_pages(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("merchant-payroll-employees"), {"name": "Citra", "ptkp_status": "TK/0",
            "base_salary": "5.500.000", "allowance": "0", "pph21_rate": "0,25", "bpjs": "1", "is_active": "1"}, follow=True)
        self.assertContains(response, "Citra")
        self.assertEqual(Employee.objects.get(name="Citra").pph21_rate, D("0.25"))
        response = self.client.post(reverse("merchant-payroll"), {"period": self.period.strftime("%Y-%m")}, follow=True)
        run = PayrollRun.objects.get(store=self.store)
        self.assertContains(response, "Citra")
        line = run.lines.get(employee=self.ani)
        self.client.post(reverse("merchant-payroll-run", args=[run.pk]), {"action": "post",
            **{f"{f}_{l.pk}": v for l in run.lines.all() for f, v in (("salary", str(int(l.salary))),
                ("allowance", str(int(l.allowance))), ("extra", "0"), ("rate", str(l.pph21_rate)))}})
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.POSTED)
        slip = self.client.get(reverse("merchant-payroll-slip", args=[run.pk, line.pk]))
        self.assertContains(slip, "Slip gaji")
        self.assertContains(slip, "PPh 21 (tarif 3%)")
        self.assertContains(slip, "Rp9.286.380")  # no bonus this time: 10.000.000 - 400.000 - 313.620
        self.assertContains(self.client.get(reverse("merchant-payroll")), run.number)

    def test_ops_cannot_see_salaries(self):
        clerk = User.objects.create_user("ops1")
        StoreStaff.objects.create(store=self.store, user=clerk, role="ops")
        self.client.force_login(clerk)
        self.assertEqual(self.client.get(reverse("merchant-payroll")).status_code, 404)

    def test_beranda_asks_to_pay_a_posted_run(self):
        from apps.accounting.todo import todo_items
        payroll.post_run(self.draft(), actor=self.owner)
        labels = [item["label"] for item in todo_items(self.store, self.today)]
        self.assertIn("penggajian sudah diposting tetapi gajinya belum dibayar", labels)
