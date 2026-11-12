"""Kas & bank, bank reconciliation, fixed assets, and the profit-and-loss and cash-flow reports."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting import assets, bank, reports
from apps.accounting.ledger import (
    CASH,
    EQUITY,
    FIXED_ASSETS,
    account_balances,
    post_backlog,
)
from apps.accounting.models import BankStatementLine, CashTransaction, FixedAsset, JournalEntry
from apps.shops.models import Store


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("books-owner", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Toko Buku", slug="toko-buku")
        self.main = bank.bank_accounts(self.store)[0]
        self.today = timezone.localdate()


class CashTests(Base):
    def test_owner_capital_and_expense_post_to_the_ledger(self):
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "Modal awal", 10_000_000)])
        bank.record_cash_transaction(self.store, kind="spend", bank_account=self.main,
            date=self.today, lines=[("8100", "Biaya admin", 6_500)])
        self.assertEqual(balance(self.store, CASH), Decimal(9_993_500))
        self.assertEqual(balance(self.store, EQUITY), Decimal(10_000_000))
        self.assertEqual(balance(self.store, "8100"), Decimal(6_500))

    def test_input_vat_is_split_out(self):
        bank.record_cash_transaction(self.store, kind="spend", bank_account=self.main,
            date=self.today, lines=[("6900", "ATK", 100_000)], tax=11_000)
        self.assertEqual(balance(self.store, "1500"), Decimal(11_000))
        self.assertEqual(balance(self.store, CASH), Decimal(-111_000))

    def test_new_accounts_get_their_own_ledger_account_and_transfers_move_money(self):
        petty = bank.add_bank_account(self.store, name="Kas kecil", kind="cash")
        self.assertEqual(petty.account.code, "1101")
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "Modal", 1_000_000)])
        bank.record_cash_transaction(self.store, kind="transfer", bank_account=self.main,
            to_bank_account=petty, amount=250_000, date=self.today)
        got = bank.balances(self.store)
        self.assertEqual(got[self.main.id], Decimal(750_000))
        self.assertEqual(got[petty.id], Decimal(250_000))
        profit = reports.profit_and_loss(self.store, self.today.replace(day=1), self.today)
        self.assertEqual(profit["totals"]["net"]["amount"], Decimal(0))

    def test_cash_lines_cannot_use_a_money_account(self):
        petty = bank.add_bank_account(self.store, name="Kas kecil")
        with self.assertRaises(bank.BankError):
            bank.record_cash_transaction(self.store, kind="spend", bank_account=self.main,
                date=self.today, lines=[(petty.account.code, "", 1000)])

    def test_void_reverses_the_entry(self):
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "Modal", 500_000)])
        bank.void_cash_transaction(txn)
        self.assertEqual(balance(self.store, CASH), Decimal(0))
        self.assertTrue(CashTransaction.objects.get(pk=txn.pk).voided)

    def test_backlog_posts_a_missed_transaction_once(self):
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "Modal", 500_000)])
        JournalEntry.objects.filter(source_type="cash", source_id=str(txn.pk)).delete()
        self.assertEqual(balance(self.store, CASH), Decimal(0))
        post_backlog()
        post_backlog()
        self.assertEqual(balance(self.store, CASH), Decimal(500_000))


class StatementTests(Base):
    CSV = ("Tanggal,Keterangan,Debit,Kredit,Saldo\n"
        "01/11/2026,TRSF dari Budi,,150.000,150.000\n"
        "02/11/2026,BIAYA ADM,6.500,,143.500\n")

    def test_parsing_handles_indonesian_amounts(self):
        rows, skipped = bank.parse_statement(self.CSV)
        self.assertEqual([r["amount"] for r in rows], [Decimal(150000), Decimal(-6500)])
        self.assertEqual(rows[1]["balance"], Decimal(143500))

    def test_bca_style_dbcr_column(self):
        text = ("Tanggal Transaksi,Keterangan,Cabang,Jumlah,,Saldo\n"
            "'05/11,TRSF CR,'0000,\"150,000.00\",CR,\"1,150,000.00\"\n"
            "'06/11,BIAYA,'0000,\"10,000.00\",DB,\"1,140,000.00\"\n")
        rows, _ = bank.parse_statement(text)
        self.assertEqual([r["amount"] for r in rows], [Decimal(150000), Decimal(-10000)])

    def test_unreadable_file_is_refused(self):
        with self.assertRaises(bank.BankError):
            bank.parse_statement("a,b,c\n1,2,3\n")

    def test_import_is_idempotent_and_keeps_identical_lines(self):
        rows, _ = bank.parse_statement(self.CSV + "02/11/2026,BIAYA ADM,6.500,,137.000\n")
        added, dup = bank.import_statement(self.main, rows)
        self.assertEqual((added, dup), (3, 0))
        added, dup = bank.import_statement(self.main, rows)
        self.assertEqual((added, dup), (0, 3))

    def test_match_suggestion_and_create(self):
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=datetime.date(2026, 11, 1), lines=[(EQUITY, "Setoran Budi", 150_000)])
        rows, _ = bank.parse_statement(self.CSV)
        bank.import_statement(self.main, rows)
        deposit, fee = BankStatementLine.objects.order_by("date")
        found = bank.suggestions(deposit)
        self.assertEqual(len(found), 1)
        bank.match_line(deposit, found[0])
        self.assertEqual(BankStatementLine.objects.get(pk=deposit.pk).status, "matched")
        with self.assertRaises(bank.BankError):  # already claimed
            bank.match_line(fee, found[0])
        bank.create_and_match(fee, account_code="8100", description="Biaya admin")
        self.assertEqual(balance(self.store, "8100"), Decimal(6_500))
        summary = bank.reconciliation_summary(self.main)
        self.assertEqual((summary["unmatched_count"], summary["matched_count"]), (0, 2))
        self.assertEqual(summary["book_balance"], summary["statement_balance"])

    def test_wrong_amount_cannot_match(self):
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "x", 100_000)])
        rows, _ = bank.parse_statement(self.CSV)
        bank.import_statement(self.main, rows)
        deposit = BankStatementLine.objects.order_by("date").first()
        line = txn.lines.first()  # noqa: F841
        from apps.accounting.models import JournalLine
        journal_line = JournalLine.objects.get(entry__source_id=str(txn.pk), account=self.main.account)
        with self.assertRaises(bank.BankError):
            bank.match_line(deposit, journal_line)

    def test_matched_transaction_cannot_be_voided(self):
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=datetime.date(2026, 11, 1), lines=[(EQUITY, "x", 150_000)])
        rows, _ = bank.parse_statement(self.CSV)
        bank.import_statement(self.main, rows)
        deposit = BankStatementLine.objects.order_by("date").first()
        bank.match_line(deposit, bank.suggestions(deposit)[0])
        with self.assertRaises(bank.BankError):
            bank.void_cash_transaction(txn)


class AssetTests(Base):
    def make(self, **kw):
        args = dict(name="Laptop", acquired_on=datetime.date(2026, 1, 15), cost=12_000_000,
            useful_life_months=48, paid_from_code=CASH, residual_value=0)
        args.update(kw)
        return assets.acquire(self.store, **args)

    def test_purchase_and_monthly_depreciation(self):
        asset = self.make()
        self.assertEqual(balance(self.store, FIXED_ASSETS), Decimal(12_000_000))
        posted = assets.depreciate(self.store, through=datetime.date(2026, 3, 31))
        self.assertEqual(len(posted), 3)
        self.assertEqual(assets.accumulated(asset), Decimal(750_000))
        self.assertEqual(assets.depreciate(self.store, through=datetime.date(2026, 3, 31)), [])
        self.assertEqual(balance(self.store, "6950"), Decimal(750_000))

    def test_last_month_absorbs_rounding_and_stops_at_residual(self):
        asset = self.make(cost=1_000_000, residual_value=100_000, useful_life_months=7)
        plan = assets.schedule(asset)
        self.assertEqual(sum(r["amount"] for r in plan), Decimal(900_000))
        self.assertEqual(plan[-1]["book_value"], Decimal(100_000))
        assets.depreciate(self.store, through=datetime.date(2030, 12, 31))
        self.assertEqual(assets.book_value(asset), Decimal(100_000))

    def test_disposal_books_gain_or_loss(self):
        asset = self.make()
        assets.depreciate(self.store, through=datetime.date(2026, 5, 31))  # 5 × 250.000
        _, gain = assets.dispose(asset, disposed_on=datetime.date(2026, 6, 20),
            proceeds=11_000_000, received_into_code=CASH)
        # June's charge is not booked on a mid-month disposal; book value 10.750.000.
        self.assertEqual(gain, Decimal(250_000))
        self.assertEqual(balance(self.store, FIXED_ASSETS), Decimal(0))
        self.assertEqual(balance(self.store, "1690"), Decimal(0))
        self.assertEqual(FixedAsset.objects.get(pk=asset.pk).status, "disposed")
        with self.assertRaises(assets.AssetError):
            assets.dispose(asset, disposed_on=datetime.date(2026, 7, 1))

    def test_invalid_inputs(self):
        with self.assertRaises(assets.AssetError):
            self.make(cost=0)
        with self.assertRaises(assets.AssetError):
            self.make(residual_value=12_000_000)


class ReportTests(Base):
    def test_profit_and_loss_and_cash_flow(self):
        day = datetime.date(2026, 10, 10)
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=day, lines=[(EQUITY, "Modal", 5_000_000), ("7100", "Bunga bank", 20_000)])
        bank.record_cash_transaction(self.store, kind="spend", bank_account=self.main,
            date=day, lines=[("6300", "Sewa", 1_000_000)])
        start, end = datetime.date(2026, 10, 1), datetime.date(2026, 10, 31)
        report = reports.profit_and_loss(self.store, start, end)
        self.assertEqual(report["totals"]["net"]["amount"], Decimal(-980_000))
        flow = reports.cash_flow(self.store, start, end)
        self.assertEqual(flow["net"], Decimal(4_020_000))
        self.assertEqual(flow["closing"], Decimal(4_020_000))
        self.assertEqual(flow["difference"], Decimal(0))
        by_key = {s["key"]: s for s in flow["sections"]}
        self.assertEqual(by_key["financing"]["net"], Decimal(5_000_000))
        self.assertEqual(by_key["operating"]["net"], Decimal(-980_000))

    def test_previous_period_is_the_month_before(self):
        self.assertEqual(reports.previous(datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)),
            (datetime.date(2026, 2, 1), datetime.date(2026, 2, 28)))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_every_page_renders(self):
        txn = bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(EQUITY, "Modal", 1_000_000)])
        asset = assets.acquire(self.store, name="Motor", acquired_on=self.today, cost=20_000_000,
            useful_life_months=96, paid_from_code=CASH)
        entry = JournalEntry.objects.filter(store=self.store).first()
        urls = [reverse("merchant-bank-accounts"), reverse("merchant-bank-account", args=[self.main.id]),
            reverse("merchant-bank-account", args=[self.main.id]) + "?tab=all",
            reverse("merchant-bank-account", args=[self.main.id]) + "?tab=book",
            reverse("merchant-cash-new"), reverse("merchant-cash-new") + "?kind=receive",
            reverse("merchant-cash-new") + "?kind=transfer",
            reverse("merchant-cash-detail", args=[txn.pk]), reverse("merchant-journal-list"),
            reverse("merchant-journal-list") + "?format=csv",
            reverse("merchant-journal-detail", args=[entry.pk]),
            reverse("merchant-account-ledger", args=[CASH]),
            reverse("merchant-account-ledger", args=[CASH]) + "?format=csv",
            reverse("merchant-assets"), reverse("merchant-asset-detail", args=[asset.pk]),
            reverse("merchant-profit-loss"), reverse("merchant-profit-loss") + "?format=csv",
            reverse("merchant-profit-loss") + "?period=last_year",
            reverse("merchant-cash-flow"), reverse("merchant-cash-flow") + "?period=this_quarter"]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_posting_through_the_form(self):
        response = self.client.post(reverse("merchant-cash-new"), {"kind": "spend",
            "bank_account": self.main.id, "date": self.today.isoformat(), "account[]": ["6900", ""],
            "line_description[]": ["Parkir", ""], "amount[]": ["25.000", ""], "tax": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance(self.store, "6900"), Decimal(25_000))

    def test_statement_import_through_the_page(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile("mutasi.csv", StatementTests.CSV.encode())
        response = self.client.post(reverse("merchant-bank-account", args=[self.main.id]),
            {"action": "import", "file": upload})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BankStatementLine.objects.count(), 2)

    def test_other_stores_data_is_not_reachable(self):
        other = User.objects.create_user("other", password="x")
        Store.objects.create(owner=other, name="Lain", slug="lain")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("merchant-bank-account", args=[self.main.id])).status_code, 404)


class StatementReportTests(Base):
    def test_balance_sheet_balances_and_includes_fixed_assets_and_profit(self):
        day = datetime.date(2026, 10, 10)
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=day, lines=[(EQUITY, "Modal", 50_000_000)])
        assets.acquire(self.store, name="Motor", acquired_on=day, cost=12_000_000,
            useful_life_months=48, paid_from_code=CASH)
        assets.depreciate(self.store, through=datetime.date(2026, 10, 31))
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=day, lines=[("7100", "Bunga", 100_000)])
        sheet = reports.balance_sheet(self.store, datetime.date(2026, 10, 31),
            compare_to=datetime.date(2026, 9, 30))
        self.assertTrue(sheet["balanced"])
        self.assertEqual(sheet["totals"]["fixed_assets"]["amount"], Decimal(11_750_000))
        profit = {r["name"]: r["amount"] for r in sheet["sections"]["equity"]}
        self.assertEqual(profit["Laba tahun berjalan (2026)"], Decimal(100_000 - 250_000))
        self.assertEqual(sheet["totals"]["assets"]["previous"], Decimal(0))

    def test_prior_year_profit_moves_to_retained_earnings(self):
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=datetime.date(2025, 6, 1), lines=[("7100", "Bunga", 400_000)])
        sheet = reports.balance_sheet(self.store, datetime.date(2026, 3, 31))
        eq = {r["name"]: r["amount"] for r in sheet["sections"]["equity"]}
        self.assertEqual(eq["Laba ditahan (tahun-tahun sebelumnya)"], Decimal(400_000))
        self.assertTrue(sheet["balanced"])

    def test_payables_aging_buckets(self):
        from apps.accounting.models import Supplier
        from apps.accounting.payables import aging, create_bill, due_soon, pay_bill
        supplier = Supplier.objects.create(store=self.store, name="CV Maju")
        today = self.today
        def bill(total, due_days):
            return create_bill(self.store, supplier=supplier, bill_date=today - datetime.timedelta(days=60),
                due_date=today + datetime.timedelta(days=due_days),
                lines=[{"description": "Jasa", "qty": 1, "unit_price": total}])
        bill(100_000, 10)      # not due
        bill(200_000, -15)     # 15 days late
        late = bill(300_000, -120)  # over 90
        pay_bill(late, amount=Decimal(100_000), paid_on=today)
        rows, totals = aging(self.store, today)
        self.assertEqual(len(rows), 1)
        self.assertEqual((totals["current"], totals["d30"], totals["over"]),
            (Decimal(100_000), Decimal(200_000), Decimal(200_000)))
        self.assertEqual(totals["total"], Decimal(500_000))
        self.assertEqual(balance(self.store, "2300"), Decimal(500_000))
        self.assertEqual(len(due_soon(self.store, today)), 2)

    def test_new_pages_render(self):
        self.client.force_login(self.owner)
        for name in ("merchant-accounting", "merchant-balance-sheet", "merchant-payables"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("merchant-balance-sheet") + "?format=csv").status_code, 200)
