"""Kalender pajak: what falls due in a month, with the amount from the books and whether it was done.

For month M the duties are the ones for the activity of M-1: PPh 21/23/4(2) deposit by the 10th and its
return by the 20th, PPh 25 and PPh Final UMKM by the 15th, PPN deposit and return by the last day. Annual
returns: SPT Tahunan orang pribadi 31 March, badan 30 April. A duty is *done* when the accountant marks it
(with the NTPN / BPE), or, for the PPh deposit, when every withholding of that month is deposited."""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse

from .models import TaxFiling

SOON = 7


def _prev(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _last(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def obligations(store, year, month, today):
    from apps.accounting.models import Withholding

    from . import fiscal, spt
    from .services import monthly_tax, tax_profile
    profile = tax_profile(store)
    py, pm = _prev(year, month)
    period = f"{py}-{pm:02d}"
    done = {f.code: f for f in TaxFiling.objects.filter(store=store).filter(
        Q(code__endswith=period) | Q(code__startswith="spt_tahunan") | Q(code__startswith="pph25"))}
    auto_done = set()
    items = []

    def add(code, title, due, amount, url, note=""):
        filing = done.get(code)
        status = ("done" if filing or code in auto_done else "overdue" if due < today else "soon" if due <= today + timedelta(days=SOON) else "upcoming")
        items.append({"code": code, "title": title, "due": due, "amount": amount, "url": url, "note": note,
            "status": status, "filing": filing})

    month_rows = Withholding.objects.filter(store=store, date__year=py, date__month=pm)
    if month_rows.exists():
        owed = sum((r.amount for r in month_rows.filter(deposit__isnull=True)), Decimal(0))
        code = f"pph_deposit:{period}"
        if not owed:
            auto_done.add(code)  # every withholding of the month is already deposited
        add(code, "Setor PPh 21/23/4(2) yang dipotong", date(year, month, 10), owed, reverse("merchant-withholdings") + f"?month={period}")
        add(f"pph_report:{period}", "Lapor SPT Masa PPh Unifikasi", date(year, month, 20), None, reverse("merchant-withholdings") + f"?month={period}",
            "Buat dari bukti potong bulan itu di Coretax.")
    if profile.pph_regime == "umkm_final" and profile.taxpayer_type:
        tax = monthly_tax(store, py, pm, profile)
        if tax.pph:
            add(f"pph_final:{period}", "Setor PPh Final UMKM 0,5%", date(year, month, 15), tax.pph, reverse("merchant-tax") + f"?month={period}")
    elif profile.taxpayer_type == "company":
        last_year = fiscal.worksheet(store, year - 1)
        if last_year["pph25_monthly"]:
            add(f"pph25:{year}-{month:02d}", "Setor PPh 25 (angsuran bulanan)", date(year, month, 15), last_year["pph25_monthly"],
                reverse("merchant-spt-tahunan") + f"?year={year - 1}", "Perkiraan dari PPh terutang tahun lalu dibagi 12.")
    if profile.is_pkp:
        sheet = spt.worksheet(store, py, pm)
        add(f"ppn:{period}", "Setor dan lapor SPT Masa PPN", _last(year, month), sheet["to_pay"], reverse("merchant-spt-ppn") + f"?month={period}",
            "Lebih bayar dikompensasikan ke masa berikutnya." if sheet["overpaid"] else "")
    if month == 3:
        add(f"spt_tahunan:op:{year - 1}", "Lapor SPT Tahunan orang pribadi", date(year, 3, 31), None, reverse("merchant-spt-tahunan") + f"?year={year - 1}")
    if month == 4:
        add(f"spt_tahunan:badan:{year - 1}", "Lapor SPT Tahunan badan (PPh 29 disetor sebelum lapor)", date(year, 4, 30), None,
            reverse("merchant-spt-tahunan") + f"?year={year - 1}")
    items.sort(key=lambda i: i["due"])
    return items


def attention(store, today):
    """Duties of the current month (and last month's late ones) that are overdue or due within a week: for Beranda."""
    year, month = today.year, today.month
    rows = obligations(store, year, month, today)
    py, pm = _prev(year, month)
    rows += [r for r in obligations(store, py, pm, today) if r["status"] == "overdue"]
    return [r for r in rows if r["status"] in ("overdue", "soon")]
