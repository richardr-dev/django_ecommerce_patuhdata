"""Quarter statements: Q1..Q4 side by side, equity statement, indirect cash flow that ties to the cash accounts."""
from datetime import date
from decimal import Decimal

from test_sales_docs import Base

from apps.accounting import adjustments, reports
from apps.accounting.ledger import (
    CASH,
    COGS,
    DRAWINGS,
    EQUITY,
    INVENTORY,
    PAYABLE,
    RECEIVABLE,
    SALES,
    post_entry,
)


def post(store, day, amounts, tag):
    return post_entry(store, date=day, memo=tag, amounts=amounts, source=("t", tag))


class QuarterTests(Base):
    def setUp(self):
        super().setUp()
        s, y = self.store, 2026
        post(s, date(y, 1, 2), {CASH: 10_000_000, EQUITY: -10_000_000}, "capital")                 # owner puts in capital
        post(s, date(y, 1, 10), {INVENTORY: 4_000_000, PAYABLE: -4_000_000}, "buy")                # stock on credit
        post(s, date(y, 2, 5), {RECEIVABLE: 6_000_000, SALES: -6_000_000}, "sale")                 # sale on credit
        post(s, date(y, 2, 5), {COGS: 2_500_000, INVENTORY: -2_500_000}, "hpp")
        post(s, date(y, 3, 1), {CASH: 3_000_000, RECEIVABLE: -3_000_000}, "collect")               # part collected
        post(s, date(y, 3, 20), {PAYABLE: 1_000_000, CASH: -1_000_000}, "pay")                     # part paid
        post(s, date(y, 4, 3), {CASH: 2_000_000, SALES: -2_000_000}, "q2 sale")                    # a cash sale in Q2
        post(s, date(y, 4, 20), {DRAWINGS: 500_000, CASH: -500_000}, "prive")                      # drawings in Q2
        adjustments.add_prepaid(s, name="Asuransi", start_date=date(y, 5, 1), months=6, total=Decimal(600_000),
            expense_code="6900", paid_from_code=CASH)
        adjustments.amortize(s, through=date(y, 6, 30))

    def test_quarters_side_by_side(self):
        table = reports.quarterly_profit(self.store, 2026)
        q1, q2, q3, q4 = table["quarters"]
        self.assertEqual((q1["revenue"], q1["cogs"], q1["gross"], q1["net"]), (6_000_000, 2_500_000, 3_500_000, 3_500_000))
        self.assertEqual(q1["gross_margin"], Decimal("58.3"))
        self.assertEqual((q2["revenue"], q2["operating"], q2["net"]), (2_000_000, 200_000, 1_800_000))   # 2 months of the 6-month prepaid
        self.assertEqual((q3["revenue"], q4["net"]), (0, 0))
        self.assertEqual(table["total"]["net"], 5_300_000)
        self.assertEqual(table["total"]["net"], reports.profit_and_loss(self.store, date(2026, 1, 1), date(2026, 12, 31),
            compare=False)["totals"]["net"]["amount"])                                                    # the quarters add up to the year

    def test_equity_statement_ties_to_the_balance_sheet(self):
        start, end = reports.quarter_bounds(2026, 2)
        eq = reports.equity_statement(self.store, start, end)
        self.assertEqual(eq["opening"], 13_500_000)                                                       # 10.0 capital + 3.5 profit
        self.assertEqual((eq["capital"], eq["drawings"], eq["profit"]), (0, -500_000, 1_800_000))
        self.assertEqual(eq["other"], 0)
        self.assertEqual(eq["closing"], 14_800_000)
        self.assertEqual(eq["closing"], reports.balance_sheet(self.store, end)["totals"]["equity"]["amount"])
        first = reports.equity_statement(self.store, *reports.quarter_bounds(2026, 1))
        self.assertEqual((first["opening"], first["capital"], first["profit"], first["closing"]), (0, 10_000_000, 3_500_000, 13_500_000))

    def test_indirect_cash_flow_agrees_with_the_cash_accounts(self):
        for q in (1, 2, 3):
            flow = reports.cash_flow_indirect(self.store, *reports.quarter_bounds(2026, q))
            self.assertEqual(flow["difference"], 0, f"Q{q} indirect cash flow does not tie to cash")
            direct = reports.cash_flow(self.store, *reports.quarter_bounds(2026, q))
            self.assertEqual(flow["net"], direct["net"])                                                 # same answer by both methods
        q1 = reports.cash_flow_indirect(self.store, *reports.quarter_bounds(2026, 1))
        operating = next(s for s in q1["sections"] if s["key"] == "operating")
        financing = next(s for s in q1["sections"] if s["key"] == "financing")
        # profit 3,5m, receivables up 3,0m, inventory up 1,5m, payables up 3,0m
        self.assertEqual(operating["net"], 3_500_000 - 3_000_000 - 1_500_000 + 3_000_000)
        self.assertEqual(financing["net"], 10_000_000)
        self.assertEqual((q1["opening"], q1["closing"]), (0, 12_000_000))                                # 10.0 + 3.0 − 1.0

    def test_fixed_assets_and_depreciation_flow_through_investing_and_the_add_back(self):
        from apps.accounting import assets
        assets.acquire(self.store, name="Laptop", acquired_on=date(2026, 7, 1), cost=Decimal(12_000_000), useful_life_months=24,
            paid_from_code=CASH)
        assets.depreciate(self.store, through=date(2026, 9, 30))
        flow = reports.cash_flow_indirect(self.store, *reports.quarter_bounds(2026, 3))
        self.assertEqual(flow["difference"], 0)
        investing = next(s for s in flow["sections"] if s["key"] == "investing")
        self.assertEqual(investing["net"], -12_000_000)
        operating = next(s for s in flow["sections"] if s["key"] == "operating")
        self.assertIn(("Penyusutan", Decimal(1_500_000)), operating["rows"])                              # 3 months x 500.000

    def test_a_closed_year_still_reports_its_quarters(self):
        from apps.accounting import closing
        closing.close_year(self.store, 2026, today=date(2027, 2, 1))
        table = reports.quarterly_profit(self.store, 2026)
        self.assertEqual(table["total"]["net"], 5_300_000)
        flow = reports.cash_flow_indirect(self.store, *reports.quarter_bounds(2026, 4))
        self.assertEqual(flow["difference"], 0)

    def test_quarter_helpers(self):
        self.assertEqual(reports.quarter_bounds(2026, 4), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertEqual(reports.quarter_of(date(2026, 5, 17)), (2026, 2))
        self.assertEqual(reports.previous_quarter(2026, 1), (2025, 4))


class QuarterPageTests(Base):
    def test_the_page_and_the_excel(self):
        import io

        from django.urls import reverse
        from openpyxl import load_workbook
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-quarter-report"), {"year": 2026, "q": 2})
        for text in ("Empat kuartal 2026", "Laba rugi kuartal 2", "Arus kas (metode tidak langsung)", "Perubahan ekuitas", "Neraca seimbang"):
            self.assertContains(page, text)
        wb = load_workbook(io.BytesIO(self.client.get(reverse("merchant-quarter-report"), {"year": 2026, "q": 2, "format": "xlsx"}).content))
        self.assertEqual(wb.sheetnames, ["Per kuartal", "Laba rugi", "Neraca", "Arus kas (tidak langsung)", "Perubahan ekuitas"])
        self.assertEqual(self.client.get(reverse("merchant-quarter-report"), {"year": "x", "q": "9"}).status_code, 200)   # bad input falls back
