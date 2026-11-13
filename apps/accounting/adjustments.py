"""Jurnal penyesuaian: the entries an accountant makes at the end of a period so the books show what
really happened, not just what was paid.

* **Biaya dibayar di muka** (prepaid): paid up front, expensed over the months it covers.
* **Akrual** and **jurnal berulang**: an expense incurred but not billed yet (accrued at period end and,
  optionally, reversed on day one of the next so the real bill books normally); a charge that repeats.
* **Cadangan kerugian piutang**: a provision against receivables that may not be collected, trued up
  each period from the aging buckets.

Everything is idempotent per source document, safe to rerun, and goes through the one ledger."""
import calendar
from datetime import date as Date
from decimal import Decimal

from django.db import IntegrityError, transaction

from .ledger import (
    ACCRUED,
    ALLOWANCE,
    BAD_DEBT,
    PREPAID,
    LedgerError,
    chart_of_accounts,
    live_entry,
    money,
    post_entry,
    register_backlog,
)
from .models import EXPENSE_CATEGORIES, Account, JournalTemplate, PrepaidExpense


class AdjustmentError(ValueError):
    pass


def month_end(year, month):
    return Date(year, month, calendar.monthrange(year, month)[1])


def add_months(day, count):
    month = day.month - 1 + count
    year, month = day.year + month // 12, month % 12 + 1
    return Date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


# ── Biaya dibayar di muka ────────────────────────────────────────────────────

def prepaid_schedule(item):
    """``[(month_start, amount)]``: equal months, the last takes the rounding."""
    each = money(item.total / item.months)
    rows, used = [], Decimal(0)
    for index in range(item.months):
        start = add_months(item.start_date.replace(day=1), index)
        amount = item.total - used if index == item.months - 1 else each
        used += amount
        rows.append((start, amount))
    return rows


def amortized(item, *, through=None):
    """What has been expensed so far, from the ledger (not from the schedule)."""
    from django.db.models import Sum

    from .models import JournalLine
    lines = JournalLine.objects.filter(entry__store=item.store, account__code=PREPAID, entry__source_type="prepaid",
        entry__source_id__startswith=f"{item.pk}:", entry__superseded=False)
    if through:
        lines = lines.filter(entry__date__lte=through)
    return lines.aggregate(c=Sum("credit", default=0))["c"]


@transaction.atomic
def add_prepaid(store, *, name, start_date, months, total, expense_code, paid_from_code, notes="", actor=None):
    total = money(total or 0)
    if not (name or "").strip():
        raise AdjustmentError("Nama wajib diisi.")
    if not months or months < 2 or months > 120:
        raise AdjustmentError("Lama manfaat 2 sampai 120 bulan. Untuk satu bulan, catat langsung sebagai biaya.")
    if total <= 0:
        raise AdjustmentError("Jumlah harus lebih dari nol.")
    if expense_code not in dict(EXPENSE_CATEGORIES):
        raise AdjustmentError("Pilih akun biaya.")
    accounts = chart_of_accounts(store)
    if paid_from_code not in accounts or accounts[paid_from_code].type not in ("asset", "liability"):
        raise AdjustmentError("Pilih akun sumber dana (kas, bank, atau utang usaha).")
    item = PrepaidExpense.objects.create(store=store, name=name.strip()[:160], start_date=start_date, months=months,
        total=total, expense_code=expense_code, paid_from=accounts[paid_from_code], notes=notes[:300], created_by=actor)
    try:
        post_prepaid_payment(item, strict=True, actor=actor)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    return item


def post_prepaid_payment(item, *, strict=False, actor=None):
    return post_entry(item.store, date=item.start_date, strict=strict, actor=actor, source=("prepaid_paid", item.pk),
        memo=f"Biaya dibayar di muka: {item.name}", amounts={PREPAID: item.total, item.paid_from.code: -item.total})


def amortize(store, *, through, actor=None):
    """Book every month of prepaid expense due up to ``through`` (month ends). Returns entries posted."""
    posted = []
    for item in PrepaidExpense.objects.filter(store=store, status=PrepaidExpense.ACTIVE, start_date__lte=through):
        for start, amount in prepaid_schedule(item):
            end = month_end(start.year, start.month)
            if end > through:
                break
            key = f"{item.pk}:{start:%Y-%m}"
            if live_entry(store, "prepaid", key):
                continue
            try:
                with transaction.atomic():
                    entry = post_entry(store, date=end, source=("prepaid", key), actor=actor,
                        memo=f"Amortisasi {item.name} · {start:%m/%Y}", amounts={item.expense_code: amount, PREPAID: -amount})
            except (IntegrityError, LedgerError):
                continue
            if entry:
                posted.append(entry)
    return posted


def end_prepaid(item):
    """Stop amortising (the cover ended early or was cancelled): the unexpensed rest stays on the books until
    a journal moves it, so nothing disappears silently."""
    item.status = PrepaidExpense.ENDED
    item.save(update_fields=["status"])
    return item


# ── Akrual and recurring journals ───────────────────────────────────────────

def _next_date(template, day):
    step = {JournalTemplate.MONTHLY: 1, JournalTemplate.QUARTERLY: 3, JournalTemplate.YEARLY: 12}[template.frequency]
    nxt = add_months(day, step)
    # period-end dates stay period-end (31 Jan → 28 Feb → 31 Mar)
    return month_end(nxt.year, nxt.month) if day == month_end(day.year, day.month) else nxt


@transaction.atomic
def add_template(store, *, kind, name, debit_code, credit_code, amount, first_date, frequency, auto_reverse=False,
        end_on=None, today=None, actor=None):
    amount = money(amount or 0)
    if not (name or "").strip():
        raise AdjustmentError("Keterangan wajib diisi.")
    if amount <= 0:
        raise AdjustmentError("Jumlah harus lebih dari nol.")
    accounts = chart_of_accounts(store)
    if debit_code not in accounts or credit_code not in accounts:
        raise AdjustmentError("Pilih akun debit dan kredit.")
    if debit_code == credit_code:
        raise AdjustmentError("Akun debit dan kredit tidak boleh sama.")
    if frequency not in dict(JournalTemplate.FREQUENCIES):
        raise AdjustmentError("Frekuensi tidak dikenal.")
    if end_on and end_on < first_date:
        raise AdjustmentError("Tanggal berakhir sebelum tanggal pertama.")
    template = JournalTemplate.objects.create(store=store, kind=kind, name=name.strip()[:160], debit_code=debit_code,
        credit_code=credit_code, amount=amount, frequency=frequency, next_run=first_date, end_on=end_on,
        auto_reverse=auto_reverse, created_by=actor)
    if today and first_date <= today:
        run_templates(store, today=today, actor=actor)
    return template


def run_templates(store, *, today, actor=None):
    """Post every template run that is due (a missed period is caught up). Returns entries posted."""
    posted = []
    for template in JournalTemplate.objects.filter(store=store, is_active=True, next_run__lte=today):
        while template.is_active and template.next_run <= today:
            day = template.next_run
            if template.end_on and day > template.end_on:
                template.is_active = False
                break
            entry = _post_run(template, day, actor)
            if entry:
                posted.append(entry)
            template.last_run = day
            if template.frequency == JournalTemplate.ONCE or (template.end_on and _next_date(template, day) > template.end_on):
                template.is_active = False
            else:
                template.next_run = _next_date(template, day)
        template.save(update_fields=["last_run", "next_run", "is_active"])
    return posted


def _post_run(template, day, actor):
    store, key = template.store, f"{template.pk}:{day}"
    try:
        with transaction.atomic():
            entry = post_entry(store, date=day, source=("jtemplate", key), actor=actor, cost_center=template.cost_center,
                memo=f"{'Akrual' if template.kind == JournalTemplate.ACCRUAL else 'Jurnal berulang'}: {template.name}",
                amounts={template.debit_code: template.amount, template.credit_code: -template.amount})
            if entry and template.auto_reverse:
                first_of_next = add_months(day.replace(day=1), 1)
                post_entry(store, date=first_of_next, source=("jtemplate_reversal", key), actor=actor, cost_center=template.cost_center,
                    memo=f"Pembalikan akrual: {template.name}",
                    amounts={template.debit_code: -template.amount, template.credit_code: template.amount})
    except (IntegrityError, LedgerError):
        return None
    return entry


def stop_template(template):
    template.is_active = False
    template.save(update_fields=["is_active"])
    return template


# ── Cadangan kerugian piutang ───────────────────────────────────────────────

DEFAULT_RATES = {"current": Decimal("0.5"), "d30": Decimal(2), "d60": Decimal(5), "d90": Decimal(20), "over": Decimal(50)}


def provision_needed(store, as_of, rates=None):
    """The allowance the receivables ask for at ``as_of``: each aging bucket times its rate. Returns
    ``{"rows", "required", "booked", "adjustment"}``; ``adjustment`` is what to book now (negative: release)."""
    from apps.tax.receivables import BUCKETS, aging

    from .ledger import account_balances
    rates = {**DEFAULT_RATES, **(rates or {})}
    _rows, totals = aging(store, as_of)
    rows, required = [], Decimal(0)
    for key, label in BUCKETS:
        base, rate = totals[key], Decimal(rates[key])
        amount = money(base * rate / 100)
        required += amount
        rows.append({"key": key, "label": label, "base": base, "rate": rate, "amount": amount})
    booked = -{a.code: a.balance for a in account_balances(store, end=as_of)}.get(ALLOWANCE, Decimal(0))
    # the allowance is a contra-asset: a credit balance, so ``balance`` is negative; booked is the positive amount held
    return {"rows": rows, "required": required, "booked": booked, "adjustment": required - booked, "rates": rates}


@transaction.atomic
def book_provision(store, *, as_of, rates=None, actor=None):
    plan = provision_needed(store, as_of, rates)
    adjustment = plan["adjustment"]
    if not adjustment:
        raise AdjustmentError("Cadangan di buku sudah sama dengan yang dibutuhkan.")
    if live_entry(store, "provision", f"{as_of}"):
        raise AdjustmentError(f"Cadangan per {as_of:%d/%m/%Y} sudah dicatat. Pilih tanggal lain.")
    amounts = {BAD_DEBT: adjustment, ALLOWANCE: -adjustment}
    try:
        return post_entry(store, date=as_of, strict=True, actor=actor, source=("provision", f"{as_of}"),
            memo=f"Cadangan kerugian piutang per {as_of:%d/%m/%Y}" + (" (penyesuaian turun)" if adjustment < 0 else ""),
            amounts=amounts)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error


@register_backlog
def post_unposted_prepaid_payments():
    posted = 0
    for item in PrepaidExpense.objects.exclude(status=PrepaidExpense.CANCELLED).select_related("store", "paid_from"):
        if not live_entry(item.store, "prepaid_paid", item.pk):
            posted += bool(post_prepaid_payment(item))
    return posted


__all__ = ["ACCRUED", "Account", "AdjustmentError", "add_prepaid", "add_template", "amortize", "book_provision",
    "provision_needed", "run_templates"]
