"""Pinjaman: schedule, drawdown and installment payments (see ``models.Loan``).

Annuity: payment = P·r / (1 − (1+r)^−n) with r the monthly rate; each month's interest is the rate on the
balance, principal is the rest, the last installment clears the rounding. Flat: interest P·rate/12 every
month on the original principal and equal principal. Interest is booked when an installment is paid (cash
basis); an accountant who wants interest accrued to period end uses a recurring accrual journal."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from .adjustments import add_months
from .ledger import INTEREST, LOANS, LedgerError, chart_of_accounts, money, post_entry
from .models import Account, Loan, LoanInstallment


class LoanError(ValueError):
    pass


def build_schedule(principal, annual_rate, months, method, start_date):
    """``[(number, due_date, principal, interest)]``; the first installment is one month after the start."""
    principal, rate = Decimal(principal), Decimal(annual_rate) / 100 / 12
    rows, balance = [], principal
    if method == "flat":
        each = (principal / months).quantize(Decimal("0.01"), ROUND_HALF_UP)
        interest = money(principal * rate)
        for n in range(1, months + 1):
            part = balance if n == months else each
            rows.append((n, add_months(start_date, n), part, interest))
            balance -= part
        return rows
    payment = money(principal / months) if rate == 0 else money(principal * rate / (1 - (1 + rate) ** -months))
    for n in range(1, months + 1):
        interest = money(balance * rate)
        part = balance if n == months else payment - interest
        rows.append((n, add_months(start_date, n), part, interest))
        balance -= part
    return rows


@transaction.atomic
def create_loan(store, *, lender, principal, annual_rate, term_months, start_date, received_into_code, method="annuity",
        notes="", actor=None):
    principal = money(principal or 0)
    if not (lender or "").strip():
        raise LoanError("Nama pemberi pinjaman wajib diisi.")
    if principal <= 0:
        raise LoanError("Jumlah pinjaman harus lebih dari nol.")
    if annual_rate is None or Decimal(annual_rate) < 0 or Decimal(annual_rate) > 100:
        raise LoanError("Bunga per tahun 0 sampai 100 persen.")
    if not term_months or term_months < 1 or term_months > 360:
        raise LoanError("Jangka waktu 1 sampai 360 bulan.")
    if method not in ("annuity", "flat"):
        raise LoanError("Metode bunga tidak dikenal.")
    accounts = chart_of_accounts(store)
    if received_into_code not in accounts or not received_into_code.startswith("11"):
        raise LoanError("Pilih kas atau rekening bank penerima.")
    loan = Loan.objects.create(store=store, lender=lender.strip()[:160], principal=principal, annual_rate=Decimal(annual_rate),
        term_months=term_months, start_date=start_date, method=method, received_into=accounts[received_into_code],
        notes=notes[:300], created_by=actor)
    LoanInstallment.objects.bulk_create(LoanInstallment(loan=loan, number=n, due_date=due, principal=part, interest=interest)
        for n, due, part, interest in build_schedule(principal, annual_rate, term_months, method, start_date))
    try:
        post_entry(store, date=start_date, strict=True, actor=actor, source=("loan", loan.pk),
            memo=f"Pencairan pinjaman {loan.lender}", amounts={received_into_code: principal, LOANS: -principal})
    except LedgerError as error:
        raise LoanError(str(error)) from error
    return loan


@transaction.atomic
def pay_installment(installment, *, paid_on, paid_from_code, actor=None):
    installment = LoanInstallment.objects.select_for_update().select_related("loan", "loan__store").get(pk=installment.pk)
    loan = installment.loan
    if installment.paid_on:
        raise LoanError("Angsuran ini sudah dibayar.")
    earlier = loan.installments.filter(number__lt=installment.number, paid_on__isnull=True).exists()
    if earlier:
        raise LoanError("Bayar angsuran sebelumnya lebih dulu.")
    accounts = chart_of_accounts(loan.store)
    if paid_from_code not in accounts or not paid_from_code.startswith("11"):
        raise LoanError("Pilih kas atau rekening bank pembayar.")
    try:
        post_entry(loan.store, date=paid_on, strict=True, actor=actor, source=("loan_payment", f"{loan.pk}:{installment.number}"),
            memo=f"Angsuran {installment.number}/{loan.term_months} {loan.lender}",
            amounts={LOANS: installment.principal, INTEREST: installment.interest, paid_from_code: -installment.amount})
    except LedgerError as error:
        raise LoanError(str(error)) from error
    installment.paid_on, installment.paid_from = paid_on, accounts[paid_from_code]
    installment.save(update_fields=["paid_on", "paid_from"])
    if not loan.installments.filter(paid_on__isnull=True).exists():
        loan.status = Loan.PAID
        loan.save(update_fields=["status"])
    return installment


def outstanding(loan):
    return sum((i.principal for i in loan.installments.filter(paid_on__isnull=True)), Decimal(0))


def overdue(store, today):
    return LoanInstallment.objects.filter(loan__store=store, paid_on__isnull=True, due_date__lt=today).select_related("loan")


__all__ = ["Account", "LoanError", "build_schedule", "create_loan", "outstanding", "overdue", "pay_installment"]
