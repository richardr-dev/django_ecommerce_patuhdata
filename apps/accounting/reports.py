"""Financial statements read straight from the ledger: profit and loss, cash flow, and the
ledger of a single account. Nothing here writes; every figure can be traced to journal lines."""
import calendar
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.urls import NoReverseMatch, reverse

from .ledger import (
    ACCUMULATED_DEPRECIATION,
    ASSET_GAIN,
    COGS,
    DEPRECIATION,
    FIXED_ASSETS,
    LOANS,
    account_balances,
    chart_of_accounts,
    is_cash_code,
)
from .models import JournalEntry, JournalLine

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def _shift_months(day, months):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return Date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


PRESETS = [("this_month", "Bulan ini"), ("last_month", "Bulan lalu"),
    ("this_quarter", "Kuartal ini"), ("this_year", "Tahun ini"), ("last_year", "Tahun lalu")]


def period(preset, today, start=None, end=None):
    """(start, end, label) for a preset, or the custom ``start``/``end``."""
    first = today.replace(day=1)
    if start and end and start <= end:
        return start, end, f"{start:%d/%m/%Y} – {end:%d/%m/%Y}"
    if preset == "last_month":
        start = _shift_months(first, -1)
        return start, first - timedelta(days=1), f"{start:%B %Y}"
    if preset == "this_quarter":
        start = first.replace(month=(today.month - 1) // 3 * 3 + 1)
        end = _shift_months(start, 3) - timedelta(days=1)
        return start, end, f"Kuartal {(today.month - 1) // 3 + 1} {today.year}"
    if preset == "this_year":
        return Date(today.year, 1, 1), Date(today.year, 12, 31), f"Tahun {today.year}"
    if preset == "last_year":
        return Date(today.year - 1, 1, 1), Date(today.year - 1, 12, 31), f"Tahun {today.year - 1}"
    end = _shift_months(first, 1) - timedelta(days=1)
    return first, end, f"{first:%B %Y}"


def previous(start, end):
    """The period of the same length just before."""
    if start.day == 1 and (end + timedelta(days=1)).day == 1:  # whole months
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        prev_start = _shift_months(start, -months)
        return prev_start, start - timedelta(days=1)
    length = end - start
    return start - length - timedelta(days=1), start - timedelta(days=1)


# ---------------------------------------------------------------------------
# Laba rugi
# ---------------------------------------------------------------------------

def _pl_sections(accounts):
    revenue = [a for a in accounts if a.type == "revenue" and not a.code.startswith("7")]
    other_income = [a for a in accounts if a.type == "revenue" and a.code.startswith("7")]
    cogs = [a for a in accounts if a.code == COGS]
    operating = [a for a in accounts if a.type == "expense" and a.code != COGS
        and not a.code.startswith("8")]
    other_expense = [a for a in accounts if a.type == "expense" and a.code.startswith("8")]
    return revenue, cogs, operating, other_income, other_expense


def profit_and_loss(store, start, end, *, compare=True):
    """Rows of the statement with this period and the one before, and the key totals."""
    chart_of_accounts(store)
    now = {a.code: a for a in account_balances(store, start=start, end=end, exclude_closing=True)}
    prev_start, prev_end = previous(start, end)
    before = {a.code: a for a in account_balances(store, start=prev_start, end=prev_end, exclude_closing=True)} \
        if compare else {}
    revenue, cogs, operating, other_income, other_expense = _pl_sections(list(now.values()))

    def rows(accounts):
        found = [{"code": a.code, "name": a.name, "amount": a.balance,
            "previous": before[a.code].balance if a.code in before else ZERO}
            for a in accounts]
        return [row for row in found if row["amount"] or row["previous"]]

    def total(section):
        return {"amount": sum((r["amount"] for r in section), ZERO),
            "previous": sum((r["previous"] for r in section), ZERO)}

    sections = {"revenue": rows(revenue), "cogs": rows(cogs), "operating": rows(operating),
        "other_income": rows(other_income), "other_expense": rows(other_expense)}
    totals = {key: total(value) for key, value in sections.items()}

    def combine(*parts):
        return {field: sum((sign * totals[key][field] for sign, key in parts), ZERO)
            for field in ("amount", "previous")}

    totals["gross"] = combine((1, "revenue"), (-1, "cogs"))
    totals["operating_profit"] = combine((1, "revenue"), (-1, "cogs"), (-1, "operating"))
    totals["net"] = combine((1, "revenue"), (-1, "cogs"), (-1, "operating"),
        (1, "other_income"), (-1, "other_expense"))
    for key in ("gross", "operating_profit", "net"):
        amount, prev = totals[key]["amount"], totals[key]["previous"]
        totals[key]["change"] = (round((amount - prev) / abs(prev) * 100) if prev else None)
    revenue_total = totals["revenue"]["amount"]
    margins = {key: (round(totals[key]["amount"] / revenue_total * 100, 1) if revenue_total else None)
        for key in ("gross", "net")}
    return {"sections": sections, "totals": totals, "margins": margins,
        "previous_start": prev_start, "previous_end": prev_end}


def monthly_profit(store, months=6, *, today):
    """Revenue, expenses and profit for each of the last ``months`` months, oldest first."""
    first = today.replace(day=1)
    rows = []
    for back in range(months - 1, -1, -1):
        start = _shift_months(first, -back)
        end = _shift_months(start, 1) - timedelta(days=1)
        accounts = account_balances(store, start=start, end=end, exclude_closing=True)
        revenue = sum((a.balance for a in accounts if a.type == "revenue"), ZERO)
        expense = sum((a.balance for a in accounts if a.type == "expense"), ZERO)
        rows.append({"month": start, "revenue": revenue, "expense": expense,
            "profit": revenue - expense})
    return rows


# ---------------------------------------------------------------------------
# Arus kas (direct method)
# ---------------------------------------------------------------------------

def cash_activity(account):
    if account.code in {FIXED_ASSETS, ACCUMULATED_DEPRECIATION}:
        return "investing"
    if account.type == "equity" or account.code == LOANS or account.code.startswith("25"):
        return "financing"
    return "operating"


CASH_LABELS = {
    "operating": "Aktivitas operasi",
    "investing": "Aktivitas investasi",
    "financing": "Aktivitas pendanaan",
}


def _cash_balance(store, *, before=None, through=None):
    lines = JournalLine.objects.filter(entry__store=store, account__code__gte="1100",
        account__code__lte="1199")
    if before:
        lines = lines.filter(entry__date__lt=before)
    if through:
        lines = lines.filter(entry__date__lte=through)
    totals = lines.aggregate(d=Sum("debit", default=0), c=Sum("credit", default=0))
    return totals["d"] - totals["c"]


def cash_flow(store, start, end):
    """Cash in and out over the period, by what the money was for. Each entry that touched a
    money account is split over its other lines, so a sale paid by transfer shows as sales
    received and PPN collected; transfers between the store's own accounts net to nothing."""
    chart_of_accounts(store)
    entries = (JournalEntry.objects.filter(store=store, date__gte=start, date__lte=end,
        lines__account__code__gte="1100", lines__account__code__lte="1199").distinct()
        .prefetch_related("lines__account"))
    flows = {}
    for entry in entries:
        lines = list(entry.lines.all())
        cash = sum((line.debit - line.credit for line in lines if is_cash_code(line.account.code)), ZERO)
        if not cash:
            continue
        others = [line for line in lines if not is_cash_code(line.account.code)]
        for line in others:
            # The entry balances, so the other lines' amounts, sign flipped, add up to the cash.
            amount = line.credit - line.debit
            if not amount:
                continue
            key = line.account.code
            if key not in flows:
                flows[key] = {"code": key, "name": line.account.name, "amount": ZERO,
                    "activity": cash_activity(line.account)}
            flows[key]["amount"] += amount
    sections = []
    for activity, label in CASH_LABELS.items():
        rows = sorted((row for row in flows.values() if row["activity"] == activity and row["amount"]),
            key=lambda row: row["code"])
        inflow = sum((row["amount"] for row in rows if row["amount"] > 0), ZERO)
        outflow = sum((row["amount"] for row in rows if row["amount"] < 0), ZERO)
        sections.append({"key": activity, "label": label, "rows": rows, "inflow": inflow,
            "outflow": outflow, "net": inflow + outflow})
    opening = _cash_balance(store, before=start)
    closing = _cash_balance(store, through=end)
    net = sum((section["net"] for section in sections), ZERO)
    return {"sections": sections, "opening": opening, "closing": closing, "net": net,
        "difference": closing - opening - net}


# ---------------------------------------------------------------------------
# Buku besar per akun
# ---------------------------------------------------------------------------

def account_ledger(account, *, start=None, end=None, limit=1000):
    """Lines on one account in date order with the running balance on its normal side."""
    debit_normal = account.type in {"asset", "expense"}
    sign = 1 if debit_normal else -1
    lines = JournalLine.objects.filter(account=account).select_related("entry")
    opening = ZERO
    if start:
        before = lines.filter(entry__date__lt=start).aggregate(d=Sum("debit", default=0),
            c=Sum("credit", default=0))
        opening = sign * (before["d"] - before["c"])
        lines = lines.filter(entry__date__gte=start)
    if end:
        lines = lines.filter(entry__date__lte=end)
    lines = list(lines.order_by("entry__date", "entry_id", "id")[:limit])
    running = opening
    total_debit = total_credit = ZERO
    for line in lines:
        running += sign * (line.debit - line.credit)
        line.running = running
        total_debit += line.debit
        total_credit += line.credit
    return {"opening": opening, "lines": lines, "closing": running, "debit": total_debit,
        "credit": total_credit, "truncated": len(lines) == limit}


# ---------------------------------------------------------------------------
# Where an entry came from
# ---------------------------------------------------------------------------

SOURCE_LABELS = {
    "invoice": "Invoice penjualan", "invoice_payment": "Pembayaran invoice",
    "bill": "Tagihan supplier", "bill_payment": "Pembayaran tagihan",
    "payment": "Pembayaran pesanan", "refund": "Refund", "payout": "Pencairan dana",
    "expense": "Biaya operasional", "journal": "Jurnal umum", "reversal": "Pembalikan",
    "cash": "Kas & bank", "asset": "Aset tetap", "asset_disposal": "Pelepasan aset",
    "depreciation": "Penyusutan", "settlement": "Settlement mitra",
    "settlement_payment": "Pembayaran settlement", "consignment": "Konsinyasi",
    "consignment_move": "Konsinyasi", "marketplace_income": "Penghasilan marketplace",
    "stock": "Persediaan", "": "Lainnya",
}


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def source_link(entry):
    """(label, url) of the document behind an entry, url None when it has no page."""
    kind, key = entry.source_type, entry.source_id
    label = SOURCE_LABELS.get(kind, kind.replace("_", " ").capitalize())
    url = None
    if kind == "invoice":
        url = _url("merchant-invoice-detail", key)
    elif kind == "bill":
        url = _url("merchant-bill-detail", key)
    elif kind == "cash":
        url = _url("merchant-cash-detail", key)
    elif kind in {"asset", "asset_disposal"}:
        url = _url("merchant-asset-detail", key)
    elif kind == "depreciation":
        url = _url("merchant-asset-detail", key.split(":")[0])
    elif kind == "settlement":
        url = _url("settlement-detail", key)
    elif kind == "payout" or entry.payout_id:
        url = _url("merchant-payout-detail", entry.payout_id or key)
    elif kind == "reversal":
        url = _url("merchant-journal-detail", key)
    elif kind == "invoice_payment":
        from apps.tax.models import InvoicePayment
        invoice_id = InvoicePayment.objects.filter(pk=key).values_list("invoice_id", flat=True).first()
        url = invoice_id and _url("merchant-invoice-detail", invoice_id)
    elif kind == "bill_payment":
        from .models import BillPayment
        bill_id = BillPayment.objects.filter(pk=key).values_list("bill_id", flat=True).first()
        url = bill_id and _url("merchant-bill-detail", bill_id)
    elif kind == "settlement_payment":
        from apps.settlements.models import SettlementPayment
        settlement_id = SettlementPayment.objects.filter(pk=key).values_list(
            "settlement_id", flat=True).first()
        url = settlement_id and _url("settlement-detail", settlement_id)
    if kind == "payment" or entry.payment_id:
        payment = entry.payment
        if payment:
            url = _url("merchant-order-detail", payment.order_id)
    return label, url


# ---------------------------------------------------------------------------
# Neraca (balance sheet)
# ---------------------------------------------------------------------------

def balance_sheet(store, as_of, *, compare_to=None):
    """Assets, liabilities and equity at ``as_of`` (and ``compare_to``). Current-year profit is
    shown inside equity, so the sheet balances without closing entries."""
    chart_of_accounts(store)
    year_start = Date(as_of.year, 1, 1)

    def earnings(accounts):
        return (sum((a.balance for a in accounts if a.type == "revenue"), ZERO)
            - sum((a.balance for a in accounts if a.type == "expense"), ZERO))

    def snapshot(day):
        end = account_balances(store, end=day, exclude_closing=True)
        current = earnings(account_balances(store, start=Date(day.year, 1, 1), end=day, exclude_closing=True))
        return {a.code: a for a in end}, current, earnings(end) - current

    now, current_profit, retained = snapshot(as_of)
    before = snapshot(compare_to) if compare_to else ({}, ZERO, ZERO)

    def rows(accounts, *, keep_zero=False):
        out = []
        for account in accounts:
            prev = before[0][account.code].balance if account.code in before[0] else ZERO
            if account.balance or prev or keep_zero:
                out.append({"code": account.code, "name": account.name, "amount": account.balance,
                    "previous": prev})
        return out

    def total(section):
        return {"amount": sum((r["amount"] for r in section), ZERO),
            "previous": sum((r["previous"] for r in section), ZERO)}

    accounts = list(now.values())
    current_assets = rows(a for a in accounts if a.type == "asset" and a.code < FIXED_ASSETS)
    fixed_assets = rows(a for a in accounts if a.type == "asset" and a.code >= FIXED_ASSETS)
    current_liabilities = rows(a for a in accounts if a.type == "liability" and a.code < LOANS)
    long_liabilities = rows(a for a in accounts if a.type == "liability" and a.code >= LOANS)
    equity = rows(a for a in accounts if a.type == "equity")
    equity += [{"code": "", "name": "Laba ditahan (tahun-tahun sebelumnya)", "amount": retained,
            "previous": before[2]},
        {"code": "", "name": f"Laba tahun berjalan ({year_start.year})", "amount": current_profit,
            "previous": before[1]}]
    sections = {"current_assets": current_assets, "fixed_assets": fixed_assets,
        "current_liabilities": current_liabilities, "long_liabilities": long_liabilities,
        "equity": equity}
    totals = {key: total(value) for key, value in sections.items()}
    totals["assets"] = {f: totals["current_assets"][f] + totals["fixed_assets"][f]
        for f in ("amount", "previous")}
    totals["liabilities"] = {f: totals["current_liabilities"][f] + totals["long_liabilities"][f]
        for f in ("amount", "previous")}
    totals["liabilities_equity"] = {f: totals["liabilities"][f] + totals["equity"][f]
        for f in ("amount", "previous")}
    difference = totals["assets"]["amount"] - totals["liabilities_equity"]["amount"]
    return {"sections": sections, "totals": totals, "difference": difference,
        "balanced": abs(difference) < Decimal("0.02")}


# ---------------------------------------------------------------------------
# Kuartal: bounds, the four quarters side by side, equity statement, indirect cash flow
# ---------------------------------------------------------------------------

def quarter_bounds(year, quarter):
    start = Date(year, (quarter - 1) * 3 + 1, 1)
    return start, _shift_months(start, 3) - timedelta(days=1)


def quarter_of(day):
    return day.year, (day.month - 1) // 3 + 1


def previous_quarter(year, quarter):
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def quarterly_profit(store, year):
    """The four quarters of ``year`` side by side: revenue, cost of goods, gross profit, operating
    expenses, other items and net profit, with margins. Closing entries are left out."""
    quarters = []
    for q in (1, 2, 3, 4):
        start, end = quarter_bounds(year, q)
        report = profit_and_loss(store, start, end, compare=False)
        t = report["totals"]
        revenue = t["revenue"]["amount"]
        quarters.append({"q": q, "start": start, "end": end, "revenue": revenue, "cogs": t["cogs"]["amount"],
            "gross": t["gross"]["amount"], "operating": t["operating"]["amount"],
            "other": t["other_income"]["amount"] - t["other_expense"]["amount"], "net": t["net"]["amount"],
            "gross_margin": round(t["gross"]["amount"] / revenue * 100, 1) if revenue else None,
            "net_margin": round(t["net"]["amount"] / revenue * 100, 1) if revenue else None})
    total = {key: sum((q[key] for q in quarters), ZERO) for key in ("revenue", "cogs", "gross", "operating", "other", "net")}
    return {"quarters": quarters, "total": total}


def equity_statement(store, start, end):
    """Laporan perubahan ekuitas: opening equity, owner's capital added, drawings, profit for the period,
    anything else (a manual journal straight to equity), closing equity. The closing figure is the balance
    sheet's own, so the statement ties to the neraca by construction."""
    day_before = start - timedelta(days=1)
    opening = balance_sheet(store, day_before)["totals"]["equity"]["amount"]
    closing = balance_sheet(store, end)["totals"]["equity"]["amount"]
    moved = {a.code: a.balance for a in account_balances(store, start=start, end=end, exclude_closing=True)}
    capital, drawings = moved.get("3100", ZERO), moved.get("3200", ZERO)
    profit = profit_and_loss(store, start, end, compare=False)["totals"]["net"]["amount"]
    other = closing - opening - capital - drawings - profit
    rows = [("Ekuitas awal", opening), ("Setoran modal", capital), ("Prive (pengambilan pemilik)", drawings),
        ("Laba (rugi) periode", profit)]
    if abs(other) >= Decimal("0.01"):
        rows.append(("Penyesuaian lain langsung ke ekuitas", other))
    return {"rows": rows, "opening": opening, "closing": closing, "capital": capital, "drawings": drawings,
        "profit": profit, "other": other}


# accounts whose change is working capital (operating), by balance sheet side
_OPERATING_ASSETS = ("1200", "1250", "1255", "1260", "1300", "1400", "1450", "1500", "1550")
_OPERATING_LIABILITIES = ("2100", "2150", "2200", "2300", "2310", "2350", "2360", "2400")


def cash_flow_indirect(store, start, end):
    """Arus kas metode tidak langsung: profit, add back what was not cash (depreciation), then the change
    in each working-capital balance, investing (fixed assets) and financing (loans, capital, drawings).
    ``difference`` must be zero: the result is checked against the actual movement of the cash accounts."""
    chart_of_accounts(store)
    moved = {a.code: a for a in account_balances(store, start=start, end=end, exclude_closing=True)}

    def change(code):  # increase of a debit-normal account, or of a credit-normal one
        a = moved.get(code)
        return a.balance if a else ZERO

    profit = profit_and_loss(store, start, end, compare=False)["totals"]["net"]["amount"]
    depreciation = change(DEPRECIATION)
    gain_on_assets = change(ASSET_GAIN)  # a non-cash item in profit; the cash is in investing below
    operating = [("Laba (rugi) bersih", profit), ("Penyusutan", depreciation)]
    if gain_on_assets:
        operating.append(("Laba (rugi) pelepasan aset", -gain_on_assets))
    for code in _OPERATING_ASSETS:
        if change(code):
            operating.append((f"{'Kenaikan' if change(code) > 0 else 'Penurunan'} {moved[code].name}", -change(code)))
    for code in _OPERATING_LIABILITIES:
        if change(code):
            operating.append((f"{'Kenaikan' if change(code) > 0 else 'Penurunan'} {moved[code].name}", change(code)))
    fixed = change(FIXED_ASSETS)
    investing = []
    if fixed:
        investing.append(("Perolehan (pelepasan) aset tetap", -fixed))
    # the part of accumulated depreciation that is not this period's charge (a disposal) is investing too
    other_accum = change(ACCUMULATED_DEPRECIATION)
    extra = other_accum + depreciation  # accumulated is a contra-asset: a charge makes it more negative
    if abs(extra) >= Decimal("0.01"):
        investing.append(("Akumulasi penyusutan aset yang dilepas", -extra))
    if gain_on_assets:
        pass  # proceeds from a disposal arrive through the cash/bank leg of its own entry
    financing = []
    for code, label in ((LOANS, "Pinjaman bank (penerimaan dikurangi pembayaran)"), ("3100", "Setoran modal"),
            ("3200", "Prive (pengambilan pemilik)")):
        if change(code):
            financing.append((label, change(code)))
    sections = []
    for key, label, rows in (("operating", "Aktivitas operasi", operating), ("investing", "Aktivitas investasi", investing),
            ("financing", "Aktivitas pendanaan", financing)):
        sections.append({"key": key, "label": label, "rows": rows, "net": sum((v for _l, v in rows), ZERO)})
    net = sum((s["net"] for s in sections), ZERO)
    opening, closing = _cash_balance(store, before=start), _cash_balance(store, through=end)
    return {"sections": sections, "net": net, "opening": opening, "closing": closing,
        "difference": closing - opening - net}
