"""PPh 21: the TER tables of PP 58/2023 and the December recalculation, with DJP's own leaflet example.

DJP leaflet "Simplifikasi penghitungan PPh Pasal 21": Tuan R, K/0, gaji Rp15.000.000 a month, iuran pensiun
Rp150.000 a month. January–November: TER 6% × 15.000.000 = 900.000 (9.900.000 in all). The year: bruto
180.000.000 − biaya jabatan 6.000.000 − iuran 1.800.000 = neto 172.200.000; PTKP K/0 58.500.000; PKP
113.700.000; PPh 5% × 60.000.000 + 15% × 53.700.000 = 11.055.000. December: 11.055.000 − 9.900.000 = 1.155.000."""
import datetime
from decimal import Decimal

from django.test import SimpleTestCase
from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import payroll, pph21
from apps.accounting.controls import withholding_tie
from apps.accounting.ledger import WITHHELD, account_balances
from apps.accounting.models import Employee, PayrollRun, PayrollSettings, Withholding

D = Decimal


class TablesTests(SimpleTestCase):
    def test_boundaries_and_categories(self):
        self.assertEqual(pph21.ter_rate("TK/0", 5_400_000), 0)  # "sampai dengan" is inclusive
        self.assertEqual(pph21.ter_rate("TK/0", 5_400_001), D("0.25"))
        self.assertEqual(pph21.ter_rate("K/0", 15_000_000), 6)  # DJP leaflet
        self.assertEqual(pph21.ter_rate("K/1", 12_000_000), 3)  # DJP article example, category B
        self.assertEqual(pph21.ter_rate("K/3", 10_000_000), D("1.5"))  # a row some websites print as 2%
        self.assertEqual(pph21.ter_rate("TK/3", 2_000_000_000), 34)
        self.assertEqual([pph21.category(s) for s in ("TK/1", "TK/2", "K/2", "K/3")], ["A", "B", "B", "C"])

    def test_tables_climb(self):
        for name, rows in pph21.TER.items():
            uppers = [u for u, _ in rows[:-1]]
            rates = [D(r) for _, r in rows]
            self.assertEqual(uppers, sorted(uppers), name)
            self.assertEqual(rates, sorted(rates), name)
            self.assertEqual((rates[0], rates[-1], rows[-1][0]), (0, 34, None), name)
        self.assertEqual({name: len(rows) for name, rows in pph21.TER.items()}, {"A": 44, "B": 40, "C": 41})

    def test_ptkp_and_pasal_17(self):
        self.assertEqual([pph21.ptkp(s) for s in ("TK/0", "TK/3", "K/0", "K/3")],
            [D(54_000_000), D(67_500_000), D(58_500_000), D(72_000_000)])
        self.assertEqual(pph21.pasal_17(D(113_700_000)), D(11_055_000))
        self.assertEqual(pph21.pasal_17(D(6_000_000_000)), D(1_794_000_000))

    def test_djp_leaflet_year_end(self):
        result = pph21.annual("K/0", gross=180_000_000, pension=1_800_000, months=12, withheld=9_900_000)
        self.assertEqual((result["net"], result["pkp"], result["tax"], result["december"]),
            (D(172_200_000), D(113_700_000), D(11_055_000), D(1_155_000)))


class PayrollYearTests(Base):
    def year(self, employee, *, bonus_month=None, bonus=0):
        runs = []
        for month in range(1, 13):
            run = payroll.create_run(self.store, datetime.date(2025, month, 1), actor=self.owner)
            if month == bonus_month:
                line = run.lines.get(employee=employee)
                payroll.update_lines(run, {line.pk: {"extra": D(bonus)}}, actor=self.owner)
            if month < 12:
                payroll.post_run(run, actor=self.owner)
            runs.append(PayrollRun.objects.get(pk=run.pk))
        return runs

    def test_the_djp_leaflet_through_twelve_payrolls(self):
        # Only the employee's pension contribution: 1% of 15.000.000 = 150.000, like the leaflet's iuran.
        PayrollSettings.objects.create(store=self.store, kes_company=0, kes_employee=0, jht_company=0,
            jht_employee=1, jkk=0, jkm=0, jp_company=0, jp_employee=0)
        tuan_r = Employee.objects.create(store=self.store, name="Tuan R", ptkp_status="K/0",
            base_salary=D(15_000_000))
        runs = self.year(tuan_r)
        january = runs[0].lines.get()
        self.assertEqual((january.pph21_method, january.pph21_rate, january.pph21), ("ter", 6, D(900_000)))
        december = runs[11].lines.get()
        self.assertEqual((december.pph21_method, december.pph21), ("annual", D(1_155_000)))
        self.assertEqual(december.net, D(15_000_000 - 150_000 - 1_155_000))
        payroll.post_run(runs[11], actor=self.owner)
        self.client.force_login(self.owner)
        slip = self.client.get(reverse("merchant-payroll-slip", args=[runs[11].pk, december.pk]))
        self.assertContains(slip, "PPh 21 Desember (hitung ulang setahun)")
        self.assertContains(slip, "Rp113.700.000")

    def test_too_much_withheld_is_returned_in_december(self):
        """TK/0, 10.000.000 a month and a 30.000.000 bonus in March: TER withholds 2% ten times and 16% once
        (8.400.000), the year owes 7.500.000, so December returns 900.000."""
        ani = Employee.objects.create(store=self.store, name="Ani", ptkp_status="TK/0",
            base_salary=D(10_000_000), bpjs=False)
        runs = self.year(ani, bonus_month=3, bonus=30_000_000)
        self.assertEqual(runs[2].lines.get().pph21, D(6_400_000))
        december = runs[11].lines.get()
        self.assertEqual((december.pph21, december.net), (D(-900_000), D(10_900_000)))
        payroll.post_run(runs[11], actor=self.owner)
        refund = Withholding.objects.get(source_type="payroll_line", source_id=str(december.pk))
        self.assertEqual(refund.amount, D(-900_000))
        self.assertEqual(balance(self.store, WITHHELD), D(7_500_000))
        ledger = {a.code: a.balance for a in account_balances(self.store)}
        self.assertEqual(withholding_tie(self.store, datetime.date(2026, 1, 5), ledger).status, "info")  # ties; old ones undeposited

    def test_leaving_mid_year_makes_that_month_the_last_period(self):
        """TK/0, 10.000.000 a month, leaves 15 May: January–April withhold 2% (800.000); the five months'
        neto 47.500.000 is under the PTKP of 54.000.000, so May returns all 800.000; June leaves her out."""
        ani = Employee.objects.create(store=self.store, name="Ani", ptkp_status="TK/0",
            base_salary=D(10_000_000), bpjs=False, left_on=datetime.date(2025, 5, 15))
        budi = Employee.objects.create(store=self.store, name="Budi", ptkp_status="TK/0",
            base_salary=D(10_000_000), bpjs=False)
        for month in range(1, 5):
            payroll.post_run(payroll.create_run(self.store, datetime.date(2025, month, 1)), actor=self.owner)
        may = payroll.create_run(self.store, datetime.date(2025, 5, 1))
        line = may.lines.get(employee=ani)
        self.assertEqual((line.pph21_method, line.pph21, line.net), ("annual", D(-800_000), D(10_800_000)))
        self.assertEqual(payroll.year_end(line)["job_cost"], D(2_500_000))
        self.assertEqual(may.lines.get(employee=budi).pph21_method, "ter")
        self.assertEqual(payroll.pph21_label(line), "PPh 21 masa terakhir (berhenti bekerja)")
        june = payroll.create_run(self.store, datetime.date(2025, 6, 1))
        self.assertEqual(list(june.lines.values_list("employee__name", flat=True)), ["Budi"])

    def test_leaving_date_from_the_form(self):
        with self.assertRaises(payroll.PayrollError):
            payroll.employee_from_form(self.store, {"name": "Ani", "ptkp_status": "TK/0",
                "joined_on": "2025-03-01", "left_on": "2025-02-01"})
        ani = payroll.employee_from_form(self.store, {"name": "Ani", "ptkp_status": "TK/0", "left_on": "2025-05-15"})
        self.assertEqual(ani.left_on, datetime.date(2025, 5, 15))

    def test_manual_rate_overrides_the_table(self):
        ani = Employee.objects.create(store=self.store, name="Ani", ptkp_status="TK/0",
            base_salary=D(10_000_000), bpjs=False, pph21_rate=D("2.5"))
        line = payroll.create_run(self.store, datetime.date(2025, 5, 1)).lines.get(employee=ani)
        self.assertEqual((line.pph21_method, line.pph21), ("manual", D(250_000)))
        payroll.update_lines(line.run, {line.pk: {"pph21_manual": None}})
        line.refresh_from_db()
        self.assertEqual((line.pph21_method, line.pph21_rate, line.pph21), ("ter", 2, D(200_000)))
