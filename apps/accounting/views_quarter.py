"""Laporan kuartal: the quarter's statements together, with Excel and print."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions

from . import exports, reports

LOGIN = "/login/"
VIEW_ROLES = permissions.roles("books.view")


@login_required(login_url=LOGIN)
def merchant_quarter_report(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    default_year, default_q = reports.previous_quarter(*reports.quarter_of(today))   # the quarter just finished
    try:
        year, quarter = int(request.GET.get("year", default_year)), int(request.GET.get("q", default_q))
        if quarter not in (1, 2, 3, 4) or not 2000 <= year <= 2100:
            raise ValueError
    except ValueError:
        year, quarter = default_year, default_q
    if request.GET.get("format") == "xlsx":
        return exports.response(exports.quarter_workbook(store, year, quarter), f"laporan-kuartal-{year}-Q{quarter}-{store.slug}.xlsx")
    start, end = reports.quarter_bounds(year, quarter)
    py, pq = reports.previous_quarter(year, quarter)
    pl = reports.profit_and_loss(store, start, end)
    sheet = reports.balance_sheet(store, end, compare_to=reports.quarter_bounds(py, pq)[1])
    flow = reports.cash_flow_indirect(store, start, end)
    table = reports.quarterly_profit(store, year)
    this_q = table["quarters"][quarter - 1]
    checks = [("Neraca seimbang", sheet["balanced"], "Total aset sama dengan liabilitas ditambah ekuitas."),
        ("Arus kas cocok dengan saldo kas", abs(flow["difference"]) < 1, "Kenaikan kas menurut metode tidak langsung sama dengan perubahan saldo kas."),
        ("Ekuitas cocok dengan neraca", True, "Ekuitas akhir pada laporan perubahan ekuitas diambil dari neraca.")]
    return render(request, "merchant/quarter_report.html", _merchant_context(request, store, year=year, quarter=quarter,
        start=start, end=end, sheet_compare=reports.quarter_bounds(py, pq)[1], prev_label=f"Kuartal {pq} {py}", pl=pl, sheet=sheet, flow=flow, table=table, this_q=this_q,
        equity=reports.equity_statement(store, start, end), checks=checks, quarters=(1, 2, 3, 4),
        years=range(today.year - 3, today.year + 1)))
