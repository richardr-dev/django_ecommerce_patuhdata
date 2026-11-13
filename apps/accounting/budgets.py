"""Anggaran: plan the costs (and sales) month by month, then watch the ledger against the plan.

A budget line is one account in one month, for the whole business or for one cost centre (cabang,
outlet, departemen). The actual figures come from the ledger itself, so they are always what the
books say: an expense posted today counts against this month's budget at once. A cost centre's
actuals are the entries posted with that centre (expenses, supplier bills, manual journals)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from .ledger import chart_of_accounts, money
from .models import Account, BudgetLine, JournalLine

WARN_AT = Decimal("0.80")
ZERO = Decimal(0)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


class BudgetError(ValueError):
    pass


def budget_accounts(store):
    """Accounts a budget can be set on: the costs, then the revenue accounts as targets."""
    chart_of_accounts(store)
    accounts = Account.objects.filter(store=store, type__in=["expense", "revenue"]).order_by("code")
    return {"expense": [a for a in accounts if a.type == "expense"],
        "revenue": [a for a in accounts if a.type == "revenue" and a.code not in {"4800", "4900"}]}


def get_budget(store, year, cost_center=None):
    """``{account code: [12 monthly amounts]}`` for the whole business or one cost centre."""
    table = {}
    for line in BudgetLine.objects.filter(store=store, year=year, cost_center=cost_center
            ).select_related("account"):
        table.setdefault(line.account.code, [ZERO] * 12)[line.month - 1] = line.amount
    return table


@transaction.atomic
def save_budget(store, year, rows, cost_center=None):
    """``rows`` maps account code to 12 amounts. Zero or blank removes the line."""
    accounts = {a.code: a for a in Account.objects.filter(store=store, type__in=["expense", "revenue"])}
    if cost_center is not None and cost_center.store_id != store.id:
        raise BudgetError("Pusat biaya tidak ditemukan.")
    saved = 0
    for code, amounts in rows.items():
        account = accounts.get(code)
        if account is None:
            raise BudgetError(f"Akun {code} tidak bisa dianggarkan.")
        for index, value in enumerate(amounts[:12]):
            value = money(value or 0)
            if value < 0:
                raise BudgetError("Anggaran tidak boleh negatif.")
            line = BudgetLine.objects.filter(store=store, year=year, month=index + 1, account=account,
                cost_center=cost_center)
            if value == 0:
                line.delete()
            else:
                line.update_or_create(store=store, year=year, month=index + 1, account=account,
                    cost_center=cost_center, defaults={"amount": value})
                saved += 1
    return saved


def actuals(store, start, end, cost_center=None):
    """``{account code: amount}`` posted between two dates: costs positive when money is spent,
    revenue positive when earned. With a cost centre only that centre's entries count."""
    lines = JournalLine.objects.filter(entry__store=store, entry__date__gte=start, entry__date__lte=end,
        account__type__in=["expense", "revenue"])
    if cost_center is not None:
        lines = lines.filter(entry__cost_center=cost_center)
    out = {}
    for row in lines.values("account__code", "account__type").annotate(d=Sum("debit"), c=Sum("credit")):
        debit, credit = row["d"] or ZERO, row["c"] or ZERO
        out[row["account__code"]] = (debit - credit) if row["account__type"] == "expense" else (credit - debit)
    return out


def _status(kind, budget, actual):
    if budget <= 0:
        return "none"
    ratio = actual / budget
    if kind == "expense":
        return "over" if ratio > 1 else "warn" if ratio >= WARN_AT else "ok"
    return "ok" if ratio >= 1 else "behind"


def report(store, year, first_month, last_month, cost_center=None):
    """Budget against actual for months ``first_month``..``last_month`` of ``year``."""
    import calendar
    import datetime
    first_month, last_month = max(1, first_month), min(12, max(first_month, last_month))
    start = datetime.date(year, first_month, 1)
    end = datetime.date(year, last_month, calendar.monthrange(year, last_month)[1])
    budget = get_budget(store, year, cost_center)
    real = actuals(store, start, end, cost_center)
    names = {a.code: a for a in Account.objects.filter(store=store, type__in=["expense", "revenue"])}
    groups = {"expense": [], "revenue": []}
    for code in sorted(set(budget) | {c for c, v in real.items() if v}):
        account = names.get(code)
        if account is None:
            continue
        planned = sum(budget.get(code, [ZERO] * 12)[first_month - 1:last_month], ZERO)
        actual = real.get(code, ZERO)
        row = {"code": code, "name": account.name, "kind": account.type, "budget": planned,
            "actual": actual, "variance": planned - actual if account.type == "expense" else actual - planned,
            "pct": round(float(actual / planned * 100)) if planned > 0 else None,
            "bar": min(round(float(actual / planned * 100)), 100) if planned > 0 and actual > 0 else 0,
            "status": _status(account.type, planned, actual), "unbudgeted": planned == 0 and actual != 0}
        groups[account.type].append(row)
    total = {}
    for kind, rows in groups.items():
        planned, actual = sum((r["budget"] for r in rows), ZERO), sum((r["actual"] for r in rows), ZERO)
        total[kind] = {"budget": planned, "actual": actual,
            "variance": (planned - actual) if kind == "expense" else (actual - planned),
            "pct": round(float(actual / planned * 100)) if planned > 0 else None,
            "status": _status(kind, planned, actual)}
    return {"start": start, "end": end, "expense": groups["expense"], "revenue": groups["revenue"],
        "total": total, "over": [r for r in groups["expense"] if r["status"] == "over"],
        "warn": [r for r in groups["expense"] if r["status"] == "warn"],
        "unbudgeted": [r for r in groups["expense"] if r["unbudgeted"]]}


def alerts(store, today):
    """Cost lines at 80% or more of this month's budget, for Beranda and the books checks."""
    result = report(store, today.year, today.month, today.month)
    return {"over": result["over"], "warn": result["warn"]}
