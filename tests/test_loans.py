"""Loans: schedule, drawdown, and installments split into principal and interest."""
from datetime import date
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import loans
from apps.accounting.ledger import CASH, INTEREST, LOANS, account_balances
from apps.accounting.models import Loan


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class LoanTests(Base):
    def make(self, **kw):
        base = {"lender": "Bank X", "principal": Decimal(12_000_000), "annual_rate": Decimal(12), "term_months": 12,
            "start_date": date(2026, 1, 1), "received_into_code": CASH, "actor": self.owner}
        return loans.create_loan(self.store, **{**base, **kw})

    def test_annuity_schedule_clears_the_loan_exactly(self):
        rows = loans.build_schedule(12_000_000, 12, 12, "annuity", date(2026, 1, 1))
        self.assertEqual(sum(r[2] for r in rows), Decimal(12_000_000))                       # principal parts add up to the loan
        self.assertEqual(rows[0][3], Decimal(120_000))                                        # 1% of 12.000.000 in month one
        self.assertEqual(rows[0][1], date(2026, 2, 1))
        payments = {r[2] + r[3] for r in rows[:-1]}
        self.assertEqual(len(payments), 1)                                                    # equal installments but the last
        self.assertLess(rows[-1][3], rows[0][3])                                              # interest falls as the balance falls

    def test_flat_and_zero_interest_schedules(self):
        flat = loans.build_schedule(12_000_000, 12, 12, "flat", date(2026, 1, 1))
        self.assertEqual({r[3] for r in flat}, {Decimal(120_000)})                            # same interest every month
        self.assertEqual(sum(r[2] for r in flat), Decimal(12_000_000))
        free = loans.build_schedule(1_200_000, 0, 12, "annuity", date(2026, 1, 1))
        self.assertEqual((sum(r[2] for r in free), sum(r[3] for r in free)), (Decimal(1_200_000), 0))

    def test_drawdown_and_paying_installments(self):
        loan = self.make()
        self.assertEqual(balances(self.store)[LOANS], Decimal(12_000_000))                    # a liability on its credit side
        first = loan.installments.get(number=1)
        loans.pay_installment(first, paid_on=date(2026, 2, 1), paid_from_code=CASH, actor=self.owner)
        b = balances(self.store)
        self.assertEqual(b[INTEREST], first.interest)
        self.assertEqual(b[LOANS], Decimal(12_000_000) - first.principal)
        self.assertEqual(loans.outstanding(loan), Decimal(12_000_000) - first.principal)
        with self.assertRaises(loans.LoanError):
            loans.pay_installment(first, paid_on=date(2026, 2, 1), paid_from_code=CASH)       # not twice
        with self.assertRaises(loans.LoanError):
            loans.pay_installment(loan.installments.get(number=3), paid_on=date(2026, 4, 1), paid_from_code=CASH)  # in order
        for item in loan.installments.filter(paid_on__isnull=True):
            loans.pay_installment(item, paid_on=item.due_date, paid_from_code=CASH)
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.PAID)
        self.assertEqual(balances(self.store)[LOANS], 0)                                      # fully repaid, nothing left

    def test_bad_input_is_refused(self):
        for bad in ({"principal": 0}, {"annual_rate": Decimal(120)}, {"term_months": 0}, {"lender": " "}, {"method": "x"},
                {"received_into_code": "2500"}):
            with self.assertRaises(loans.LoanError):
                self.make(**bad)
        self.assertEqual(Loan.objects.count(), 0)

    def test_overdue_and_pages(self):
        loan = self.make(start_date=date(2025, 1, 1))
        self.assertEqual(loans.overdue(self.store, self.today).count(), 12)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-loans")), "Bank X")
        page = self.client.get(reverse("merchant-loan", args=[loan.pk]))
        self.assertContains(page, "Jadwal angsuran")
        self.client.post(reverse("merchant-loan", args=[loan.pk]), {"installment": loan.installments.get(number=1).pk,
            "paid_on": "2025-02-01", "paid_from": CASH})
        self.assertTrue(loan.installments.get(number=1).paid_on)
        form = {"lender": "Bank Y", "principal": "5.000.000", "annual_rate": "10", "term_months": "6", "start_date": "2026-03-01",
            "method": "annuity", "received_into": CASH, "idem": "l1"}
        self.client.post(reverse("merchant-loans"), form)
        self.client.post(reverse("merchant-loans"), form)
        self.assertEqual(Loan.objects.filter(lender="Bank Y").count(), 1)
