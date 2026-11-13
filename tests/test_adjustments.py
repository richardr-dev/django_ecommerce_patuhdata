"""Adjusting entries: prepaid amortisation, accruals that reverse themselves, recurring journals, provision."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import adjustments
from apps.accounting.ledger import ACCRUED, ALLOWANCE, BAD_DEBT, CASH, PREPAID, account_balances
from apps.accounting.models import JournalTemplate, PrepaidExpense


def balances(store, **kw):
    return {a.code: a.balance for a in account_balances(store, **kw)}


class PrepaidTests(Base):
    def test_paid_once_expensed_over_the_months(self):
        item = adjustments.add_prepaid(self.store, name="Asuransi", start_date=date(2026, 1, 15), months=12,
            total=Decimal(1_200_000), expense_code="6900", paid_from_code=CASH, actor=self.owner)
        self.assertEqual(balances(self.store)[PREPAID], Decimal(1_200_000))
        adjustments.amortize(self.store, through=date(2026, 3, 31))
        b = balances(self.store)
        self.assertEqual((b[PREPAID], b["6900"]), (Decimal(900_000), Decimal(300_000)))     # Jan, Feb, Mar
        self.assertEqual(adjustments.amortized(item), Decimal(300_000))
        self.assertEqual(adjustments.amortize(self.store, through=date(2026, 3, 31)), [])    # rerun books nothing
        adjustments.amortize(self.store, through=date(2026, 12, 31))
        b = balances(self.store)
        self.assertEqual((b[PREPAID], b["6900"]), (0, Decimal(1_200_000)))                   # fully expensed, asset at zero

    def test_the_last_month_takes_the_rounding_and_validation(self):
        item = adjustments.add_prepaid(self.store, name="Lisensi", start_date=date(2026, 1, 1), months=3,
            total=Decimal(100_000), expense_code="6800", paid_from_code=CASH)
        self.assertEqual([a for _s, a in adjustments.prepaid_schedule(item)], [Decimal("33333.33"), Decimal("33333.33"), Decimal("33333.34")])
        for bad in ({"months": 1}, {"total": 0}, {"name": " "}, {"expense_code": "4100"}, {"paid_from_code": "4100"}):
            with self.assertRaises(adjustments.AdjustmentError):
                adjustments.add_prepaid(self.store, **{"name": "x", "start_date": date(2026, 1, 1), "months": 6, "total": Decimal(600),
                    "expense_code": "6900", "paid_from_code": CASH, **bad})

    def test_a_stopped_item_keeps_its_unexpensed_rest_visible(self):
        item = adjustments.add_prepaid(self.store, name="Sewa", start_date=date(2026, 1, 1), months=12, total=Decimal(1_200_000),
            expense_code="6300", paid_from_code=CASH)
        adjustments.amortize(self.store, through=date(2026, 2, 28))
        adjustments.end_prepaid(item)
        adjustments.amortize(self.store, through=date(2026, 12, 31))
        self.assertEqual(balances(self.store)[PREPAID], Decimal(1_000_000))                   # nothing vanished


class TemplateTests(Base):
    def test_an_accrual_posts_at_period_end_and_reverses_on_day_one(self):
        adjustments.add_template(self.store, kind="accrual", name="Listrik Maret", debit_code="6600", credit_code=ACCRUED,
            amount=Decimal(500_000), first_date=date(2026, 3, 31), frequency="once", auto_reverse=True, today=date(2026, 3, 31))
        self.assertEqual(balances(self.store, end=date(2026, 3, 31))[ACCRUED], Decimal(500_000))
        self.assertEqual(balances(self.store, end=date(2026, 3, 31))["6600"], Decimal(500_000))
        april = balances(self.store, end=date(2026, 4, 1))
        self.assertEqual((april[ACCRUED], april["6600"]), (0, 0))                              # the reversal cancels it
        self.assertFalse(JournalTemplate.objects.get().is_active)                              # a one-off is done

    def test_without_reversal_the_accrual_stays(self):
        adjustments.add_template(self.store, kind="accrual", name="Bonus", debit_code="6200", credit_code=ACCRUED,
            amount=Decimal(1_000_000), first_date=date(2026, 3, 31), frequency="once", auto_reverse=False, today=date(2026, 4, 5))
        self.assertEqual(balances(self.store)[ACCRUED], Decimal(1_000_000))

    def test_a_monthly_journal_catches_up_missed_months_once(self):
        adjustments.add_template(self.store, kind="recurring", name="Alokasi", debit_code="6300", credit_code=CASH,
            amount=Decimal(100_000), first_date=date(2026, 1, 31), frequency="monthly", today=date(2026, 1, 1))
        self.assertEqual(balances(self.store).get("6300", 0), 0)                               # not due yet
        posted = adjustments.run_templates(self.store, today=date(2026, 4, 15))
        self.assertEqual(len(posted), 3)                                                       # Jan 31, Feb 28, Mar 31
        self.assertEqual(adjustments.run_templates(self.store, today=date(2026, 4, 15)), [])
        template = JournalTemplate.objects.get()
        self.assertEqual(template.next_run, date(2026, 4, 30))                                 # period ends stay period ends
        self.assertEqual(balances(self.store)["6300"], Decimal(300_000))

    def test_end_date_stops_it_and_bad_input_is_refused(self):
        adjustments.add_template(self.store, kind="recurring", name="Sementara", debit_code="6300", credit_code=CASH, amount=Decimal(10),
            first_date=date(2026, 1, 31), frequency="monthly", end_on=date(2026, 2, 28), today=date(2026, 6, 1))
        self.assertEqual(balances(self.store)["6300"], Decimal(20))
        self.assertFalse(JournalTemplate.objects.get().is_active)
        for bad in ({"amount": 0}, {"credit_code": "6300"}, {"debit_code": "9999"}, {"frequency": "daily"}, {"name": ""}):
            with self.assertRaises(adjustments.AdjustmentError):
                adjustments.add_template(self.store, **{"kind": "accrual", "name": "x", "debit_code": "6300", "credit_code": CASH,
                    "amount": Decimal(1), "first_date": date(2026, 1, 1), "frequency": "once", **bad})

    def test_the_daily_worker_runs_both(self):
        adjustments.add_prepaid(self.store, name="Asuransi", start_date=self.today.replace(day=1) - timedelta(days=40), months=12,
            total=Decimal(1_200_000), expense_code="6900", paid_from_code=CASH)
        call_command("run_books", verbosity=0)
        self.assertGreater(balances(self.store).get("6900", 0), 0)


class ProvisionTests(Base):
    def test_the_allowance_follows_the_aging_and_trues_up(self):
        self.invoice(kabel=2, lampu=0, credit=True, days_ago=120, due_days=30)             # 90 days overdue
        plan = adjustments.provision_needed(self.store, self.today)
        self.assertGreater(plan["required"], 0)
        self.assertEqual(plan["adjustment"], plan["required"])
        entry = adjustments.book_provision(self.store, as_of=self.today, actor=self.owner)
        self.assertTrue(entry)
        b = balances(self.store)
        self.assertEqual(b[BAD_DEBT], plan["required"])
        self.assertEqual(b[ALLOWANCE], -plan["required"])                                      # contra-asset: a credit balance
        self.assertEqual(adjustments.provision_needed(self.store, self.today)["adjustment"], 0)
        with self.assertRaises(adjustments.AdjustmentError):
            adjustments.book_provision(self.store, as_of=self.today)                          # nothing to adjust
        again = adjustments.provision_needed(self.store, self.today, {"d90": 0, "over": 0, "d60": 0, "d30": 0, "current": 0})
        self.assertEqual(again["adjustment"], -plan["required"])                               # lower rates release it
        adjustments.book_provision(self.store, as_of=self.today + timedelta(days=1), actor=self.owner,
            rates={"d90": 0, "over": 0, "d60": 0, "d30": 0, "current": 0})
        self.assertEqual(balances(self.store)[ALLOWANCE], 0)


class AdjustmentPageTests(Base):
    def test_the_three_tabs_and_creating_through_the_page(self):
        self.client.force_login(self.owner)
        for tab, text in (("prepaid", "Biaya dibayar di muka"), ("accrual", "Akrual dan jurnal berulang"), ("provision", "Cadangan dibutuhkan")):
            self.assertContains(self.client.get(reverse("merchant-adjustments"), {"tab": tab}), text)
        form = {"action": "add_prepaid", "tab": "prepaid", "name": "Asuransi", "start_date": self.today.isoformat(), "months": "12",
            "total": "1200000", "expense_code": "6900", "paid_from": CASH, "idem": "pp1"}
        self.client.post(reverse("merchant-adjustments"), form)
        self.client.post(reverse("merchant-adjustments"), form)
        self.assertEqual(PrepaidExpense.objects.count(), 1)                                    # double submit made one
        self.client.post(reverse("merchant-adjustments"), {"action": "add_accrual", "tab": "accrual", "name": "Listrik", "debit_code": "6600",
            "credit_code": ACCRUED, "amount": "250000", "first_date": self.today.isoformat(), "frequency": "once", "auto_reverse": "1", "idem": "ac1"})
        self.assertEqual(JournalTemplate.objects.count(), 1)
        self.assertContains(self.client.get(reverse("merchant-adjustments"), {"tab": "accrual"}), "Listrik")


class QuarterChecklistTests(Base):
    def test_quarter_checklist_adds_the_adjusting_steps_and_the_page_toggles(self):
        from apps.accounting import closing
        steps, first, last = closing.quarter_checklist(self.store, 2026, 1, date(2026, 4, 10))
        self.assertEqual((first, last), (date(2026, 1, 1), date(2026, 3, 31)))
        titles = [s["title"] for s in steps]
        for expected in ("Biaya dibayar di muka sudah dibebankan", "Akrual dan jurnal berulang sudah dijalankan",
                "Cadangan kerugian piutang ditinjau", "Laporan kuartal cocok (neraca seimbang, arus kas sama dengan kas)"):
            self.assertIn(expected, titles)
        self.assertEqual(sum(1 for t in titles if t.startswith("SPT Masa PPN")), 3)
        adjustments.add_prepaid(self.store, name="A", start_date=date(2026, 1, 1), months=6, total=Decimal(600_000),
            expense_code="6900", paid_from_code=CASH)
        steps, _f, _l = closing.quarter_checklist(self.store, 2026, 1, date(2026, 4, 10))
        step = next(s for s in steps if s["title"] == "Biaya dibayar di muka sudah dibebankan")
        self.assertFalse(step["done"])                                                         # three months not amortised yet
        adjustments.amortize(self.store, through=date(2026, 3, 31))
        steps, _f, _l = closing.quarter_checklist(self.store, 2026, 1, date(2026, 4, 10))
        self.assertTrue(next(s for s in steps if s["title"] == "Biaya dibayar di muka sudah dibebankan")["done"])
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-year-close"), {"scope": "quarter", "year": 2026, "q": 1})
        self.assertContains(page, "Tutup kuartal")
        self.assertContains(page, "Kunci sampai 31 Mar 2026")
