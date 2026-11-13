"""Year-end closing: profit to Laba ditahan, lock, reopen, and reports that survive it."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import closing, controls, reports
from apps.accounting.ledger import (
    CASH,
    COGS,
    RETAINED,
    SALES,
    LedgerLocked,
    account_balances,
    post_entry,
)
from apps.accounting.models import JournalEntry, LedgerLock


def balances(store, **kw):
    return {a.code: a.balance for a in account_balances(store, **kw)}


class YearCloseTests(Base):
    def setUp(self):
        super().setUp()
        self.today = datetime.date(2027, 3, 10)
        for day, amounts, tag in [
                (datetime.date(2026, 5, 1), {CASH: 1000, SALES: -1000}, "s1"),
                (datetime.date(2026, 6, 1), {COGS: 300, CASH: -300}, "c1")]:
            post_entry(self.store, date=day, memo=tag, amounts=amounts, source=("test", tag))

    def test_close_moves_profit_to_retained_and_locks(self):
        _entry, profit = closing.close_year(self.store, 2026, today=self.today, actor=self.owner)
        self.assertEqual(profit, Decimal(700))
        full = balances(self.store)
        self.assertEqual(full[RETAINED], Decimal(700))
        self.assertEqual(full[SALES], 0)
        self.assertEqual(full[COGS], 0)
        self.assertEqual(LedgerLock.objects.get(store=self.store).locked_through, datetime.date(2026, 12, 31))
        self.assertEqual(closing.closed_years(self.store), [2026])

    def test_reports_still_show_the_closed_years_profit(self):
        closing.close_year(self.store, 2026, today=self.today)
        pl = reports.profit_and_loss(self.store, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        self.assertEqual(pl["totals"]["net"]["amount"], Decimal(700))
        sheet = reports.balance_sheet(self.store, datetime.date(2026, 12, 31))
        self.assertTrue(sheet["balanced"])
        self.assertEqual(sheet["totals"]["equity"]["amount"], Decimal(700))
        later = reports.balance_sheet(self.store, datetime.date(2027, 2, 1))
        self.assertTrue(later["balanced"])

    def test_cannot_close_twice_current_year_or_out_of_order(self):
        with self.assertRaises(closing.ClosingError):
            closing.close_year(self.store, 2027, today=self.today)
        closing.close_year(self.store, 2026, today=self.today)
        with self.assertRaises(closing.ClosingError):
            closing.close_year(self.store, 2026, today=self.today)
        post_entry(self.store, date=datetime.date(2027, 1, 5), memo="n", amounts={CASH: 5, SALES: -5},
            source=("test", "n"))
        with self.assertRaises(closing.ClosingError):
            closing.close_year(self.store, 2028, today=datetime.date(2029, 1, 1))

    def test_locked_year_refuses_strict_postings(self):
        closing.close_year(self.store, 2026, today=self.today)
        with self.assertRaises(LedgerLocked):
            post_entry(self.store, date=datetime.date(2026, 7, 1), memo="late", strict=True,
                amounts={CASH: 1, SALES: -1}, source=("test", "late"))

    def test_reopen_reverses_and_unlocks_only_the_latest_year(self):
        closing.close_year(self.store, 2026, today=self.today)
        closing.reopen_year(self.store, 2026, actor=self.owner)
        self.assertFalse(LedgerLock.objects.filter(store=self.store).exists())
        self.assertEqual(balances(self.store)[RETAINED], 0)
        self.assertEqual(balances(self.store)[SALES], Decimal(1000))
        self.assertEqual(closing.closed_years(self.store), [])
        pl = reports.profit_and_loss(self.store, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        self.assertEqual(pl["totals"]["net"]["amount"], Decimal(700))
        closing.close_year(self.store, 2026, today=self.today)  # can close again
        with self.assertRaises(closing.ClosingError):
            closing.reopen_year(self.store, 2025)

    def test_control_fails_when_profit_changes_after_closing(self):
        closing.close_year(self.store, 2026, today=self.today)
        self.assertEqual(controls.year_close_tie(self.store, self.today, {}).status, controls.OK)
        LedgerLock.objects.filter(store=self.store).delete()  # someone lifted the lock
        entry = post_entry(self.store, date=datetime.date(2026, 8, 1), memo="sneaky",
            amounts={CASH: 50, SALES: -50}, source=("test", "sneaky"))
        self.assertIsNotNone(entry)
        self.assertEqual(controls.year_close_tie(self.store, self.today, {}).status, controls.FAIL)

    def test_page_and_actions(self):
        self.client.force_login(self.owner)
        from django.utils import timezone
        last_year = timezone.localdate().year - 1
        JournalEntry.objects.filter(store=self.store).update(date=datetime.date(last_year, 5, 1))
        page = self.client.get(reverse("merchant-year-close"))
        self.assertContains(page, "Tutup tahun")
        self.assertContains(page, "Checklist tutup bulan")
        response = self.client.post(reverse("merchant-year-close"), {"year": str(last_year)}, follow=True)
        self.assertTrue(JournalEntry.objects.filter(store=self.store, source_type="year_close").exists())
        self.assertContains(response, "Buka kembali")
