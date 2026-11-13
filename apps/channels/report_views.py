import csv
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.orders.models import Order
from apps.orders.views import _merchant_context, _require_roles, _staff_store

from . import analytics, sales_export
from .invoice_recap import invoice_recap
from .reporting import sales_report

REPORT_ROLES = {"admin", "manager", "finance", "ops", "viewer"}


def _period(request):
    today = timezone.localdate()
    start = parse_date(request.GET.get("start", "")) or today.replace(day=1)
    end = parse_date(request.GET.get("end", "")) or today
    if end < start:
        start, end = end, start
    if (end - start).days > 366:
        start = end - timedelta(days=366)
    return start, end


@login_required(login_url="/merchant/login/")
def merchant_sales_overview(request):
    """Laporan penjualan semua channel: penjualan, batal/retur, HPP, biaya marketplace, margin."""
    store = _staff_store(request)
    _require_roles(request, store, REPORT_ROLES)
    start, end = _period(request)
    channel = request.GET.get("channel") or None
    if channel not in {code for code, _ in Order.CHANNELS}:
        channel = None
    report = sales_report(store, start, end, channel=channel)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="penjualan-{start}-{end}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Channel", "Pesanan", "Penjualan kotor", "Diskon", "Penjualan bersih",
            "Ongkir", "Retur", "Batal", "HPP", "Biaya marketplace", "Margin", "Margin %"])
        for row in report["channels"]:
            writer.writerow([row["label"], row["orders"], row["gross"], row["discount"],
                row["net_sales"], row["shipping"], row["returns"], row["cancelled_count"],
                row["cogs"], row["fees"], row["margin"], row["margin_pct"]])
        return response
    return render(request, "merchant/sales_overview.html", _merchant_context(request, store,
        report=report, start=start, end=end, channel=channel, channels=Order.CHANNELS,
        today=date.today()))


@login_required(login_url="/merchant/login/")
def merchant_channel_recap(request):
    """Rekap invoice per channel: Shopee, Tokopedia, TikTok Shop, Lazada, mitra, toko online…"""
    from apps.accounting import reports as period_tools
    store = _staff_store(request)
    _require_roles(request, store, REPORT_ROLES)
    today = timezone.localdate()
    preset = request.GET.get("period", "this_month")
    start, end, label = period_tools.period(preset, today, parse_date(request.GET.get("start", "")),
        parse_date(request.GET.get("end", "")))
    if request.GET.get("start") and request.GET.get("end"):
        preset = "custom"
    end = min(end, today) if preset != "custom" else end
    recap = invoice_recap(store, start, end, today=today)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="rekap-channel-{start}-{end}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Channel", "Invoice", "Ditagihkan", "PPN", "Nota kredit", "Bersih",
            "Dibayar", "Belum dibayar", "Lewat jatuh tempo", "Dibatalkan"])
        for row in recap["channels"]:
            writer.writerow([row["label"], row["count"], row["invoiced"], row["tax"],
                row["credited"], row["net"], row["paid"], row["outstanding"], row["overdue"],
                row["void_count"]])
        totals = recap["totals"]
        writer.writerow(["Total", totals["count"], totals["invoiced"], totals["tax"],
            totals["credited"], totals["net"], totals["paid"], totals["outstanding"],
            totals["overdue"], totals["void_count"]])
        return response
    return render(request, "merchant/channel_recap.html", _merchant_context(request, store,
        recap=recap, start=start, end=end, label=label, preset=preset,
        presets=period_tools.PRESETS))


class _Echo:
    def write(self, value):
        return value


def _stream_csv(dataset, orders, filename):
    from django.http import StreamingHttpResponse
    writer = csv.writer(_Echo())

    def generate():
        yield "\ufeff"  # byte order mark, so Excel reads the accents
        yield writer.writerow(sales_export.DATASETS[dataset][1])
        for row in sales_export.rows(dataset, orders):
            yield writer.writerow(row)
    response = StreamingHttpResponse(generate(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return response


def _workbook(orders, filename):
    import io

    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    book = Workbook(write_only=True)
    for dataset, (title, columns) in sales_export.DATASETS.items():
        sheet = book.create_sheet(title)
        sheet.freeze_panes = "A2"
        head = []
        for name in columns:
            cell = WriteOnlyCell(sheet, value=name)
            cell.font = Font(bold=True)
            head.append(cell)
        sheet.append(head)
        for row in sales_export.rows(dataset, orders):
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-"
        "officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return response


@login_required(login_url="/merchant/login/")
def merchant_sales_export(request):
    """Ekspor data penjualan lengkap: baris barang, per pesanan, dan pembayaran, semua channel."""
    from apps.accounting import reports as period_tools
    store = _staff_store(request)
    _require_roles(request, store, REPORT_ROLES)
    today = timezone.localdate()
    preset = request.GET.get("period", "this_month")
    first = Order.objects.filter(store=store).order_by("created_at").values_list(
        "created_at", flat=True).first()
    if preset == "all":
        start, end, label = (timezone.localtime(first).date() if first else today), today, "Semua waktu"
    else:
        start, end, label = period_tools.period(preset, today, parse_date(request.GET.get("start", "")),
            parse_date(request.GET.get("end", "")))
        if request.GET.get("start") and request.GET.get("end"):
            preset = "custom"
    channel = request.GET.get("channel") or None
    if channel not in {code for code, _ in Order.CHANNELS}:
        channel = None
    only_sold = request.GET.get("sold") == "1"
    orders = sales_export.orders_in(store, start, end, channel=channel, only_sold=only_sold)
    name = f"penjualan-{store.slug}-{start}-{end}"
    dataset, fmt = request.GET.get("dataset"), request.GET.get("format")
    if fmt == "xlsx":
        return _workbook(orders, name)
    if fmt == "csv" and dataset in sales_export.DATASETS:
        return _stream_csv(dataset, orders, f"{name}-{dataset}")
    totals = orders.aggregate(total=Sum("total"), tax=Sum("tax"), shipping=Sum("shipping_cost"))
    return render(request, "merchant/sales_export.html", _merchant_context(request, store,
        counts=sales_export.counts(orders), totals=totals, start=start, end=end, label=label,
        preset=preset, presets=period_tools.PRESETS + [("all", "Semua waktu")], channel=channel,
        channels=Order.CHANNELS, only_sold=only_sold, datasets=sales_export.DATASETS))


@login_required(login_url="/merchant/login/")
def merchant_sales_analytics(request):
    """Analitik penjualan: omzet, channel, pelanggan baru dan lama, lokasi, produk, dan tren."""
    from urllib.parse import urlencode

    from apps.accounting.views import render_partial

    from .invoice_filters import PRESETS, values

    store = _staff_store(request)
    _require_roles(request, store, REPORT_ROLES)
    today = timezone.localdate()
    data = analytics.analyze(store, request.GET, today)
    used = set(Order.objects.filter(store=store).values_list("channel", flat=True))
    selected = values(request.GET, "channel")
    return render_partial(request, "merchant/sales_analytics.html", "merchant/_sales_analytics_body.html",
        _merchant_context(request, store, a=data, presets=PRESETS,
        range_preset=request.GET.get("range") or ("" if any(request.GET.get(k) for k in (
            "date_from", "date_to", "month")) else "30d"),
        date_from=request.GET.get("date_from", ""), date_to=request.GET.get("date_to", ""),
        channel_choices=[(c, n) for c, n in Order.CHANNELS if c in used], channel_selected=selected,
        channel_qs=urlencode([("channel", c) for c in selected])))


@login_required(login_url="/merchant/login/")
def merchant_sales_analytics_xlsx(request):
    """The analytics dashboard as an Excel workbook: one sheet for each table, same filters."""
    import io

    import openpyxl
    from openpyxl.styles import Font, PatternFill

    store = _staff_store(request)
    _require_roles(request, store, REPORT_ROLES)
    a = analytics.analyze(store, request.GET, timezone.localdate())
    book = openpyxl.Workbook()

    def sheet(title, header, rows, money=()):
        ws = book.create_sheet(title)
        ws.append(header)
        for cell in ws[1]:
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1A66D9")
        for row in rows:
            ws.append([float(v) if hasattr(v, "quantize") else v for v in row])
        for index in money:
            for cell in ws.iter_rows(min_row=2, min_col=index + 1, max_col=index + 1):
                cell[0].number_format = '"Rp"#,##0'
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = max(12, min(40, max(
                len(str(c.value or "")) for c in column) + 2))
        ws.freeze_panes = "A2"

    book.remove(book.active)
    sheet("Ringkasan", ["Ukuran", "Nilai"], [
        ["Periode", f"{a['start']:%d %b %Y} - {a['end']:%d %b %Y}"], ["Omzet", a["revenue"]],
        ["Omzet periode sebelumnya", a["previous_revenue"]], ["Pesanan", a["orders"]],
        ["Pembeli", a["buyers"]], ["Pembeli baru", a["new"]], ["Pembeli kembali", a["returning"]],
        ["Rata-rata per pesanan", a["avg"]], ["Batal (%)", a["cancel_pct"]], ["Retur (%)", a["return_pct"]],
        ["Margin", a["margin"]["total"]["margin"]], ["Margin (%)", a["margin"]["total"]["margin_pct"]]])
    sheet("Tren", ["Mulai" if a["weekly"] else "Tanggal", "Pesanan", "Omzet"],
        [[s["label"].isoformat(), s["orders"], s["revenue"]] for s in a["series"]], money=(2,))
    sheet("Channel", ["Channel", "Pesanan", "Pembeli", "Omzet", "Porsi (%)", "Rata-rata"],
        [[c["label"], c["orders"], c["buyers"], c["revenue"], c["share"], c["avg"]] for c in a["channels"]],
        money=(3, 5))
    sheet("Margin", ["Channel", "Penjualan bersih", "Harga pokok", "Biaya marketplace", "Margin", "Margin (%)"],
        [[r["label"], r["net_sales"], r["cogs"], r["fees"], r["margin"], r["margin_pct"]]
            for r in a["margin"]["rows"]], money=(1, 2, 3, 4))
    sheet("Provinsi", ["Provinsi", "Pesanan", "Pembeli", "Omzet"],
        [[p["name"], p["orders"], p["buyers"], p["revenue"]] for p in a["provinces"]]
        + ([["Tidak diketahui", a["unknown_province"]["orders"], a["unknown_province"]["buyers"],
            a["unknown_province"]["revenue"]]] if a["unknown_province"] else []), money=(3,))
    sheet("Kota", ["Kota", "Provinsi", "Pesanan", "Pembeli", "Omzet"],
        [[c["name"], c["province"], c["orders"], c["buyers"], c["revenue"]] for c in a["cities"]], money=(4,))
    sheet("Produk", ["Produk", "SKU", "Unit", "Pesanan", "Omzet"],
        [[p["name"], p["sku"], p["units"], p["orders"], p["revenue"]] for p in a["products"]], money=(4,))
    sheet("Pelanggan", ["Pelanggan", "Pesanan", "Omzet", "Channel"],
        [[c["name"], c["orders"], c["revenue"], ", ".join(c["channels"])] for c in a["customers"]], money=(2,))
    out = io.BytesIO()
    book.save(out)
    response = HttpResponse(out.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = (f'attachment; filename="analitik-penjualan-{store.slug}-'
        f'{a["start"]:%Y%m%d}-{a["end"]:%Y%m%d}.xlsx"')
    return response
