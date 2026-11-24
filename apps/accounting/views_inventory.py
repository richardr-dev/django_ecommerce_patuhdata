"""Kartu stok, stok lambat bergerak, and Penutupan persediaan."""
import datetime

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.catalog.models import Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions

from . import exports, inventory_reports

LOGIN = "/login/"
VIEW_ROLES = permissions.roles("books.view") | permissions.roles("stock.fulfil")


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@login_required(login_url=LOGIN)
def merchant_stock_card(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    query = request.GET.get("sku", "").strip()
    product = None
    if query:
        product = (Product.objects.filter(store=store, sku__iexact=query).first()
            or Product.objects.filter(store=store, name__icontains=query).order_by("name").first())
    start, end = _date(request.GET.get("start")), _date(request.GET.get("end"))
    card = inventory_reports.stock_card(store, product, start=start, end=end) if product else None
    if card and request.GET.get("format") == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        sheet = exports.Sheet(wb, "Kartu stok", store, f"{product.sku} · {product.name}", first=True)
        sheet.header("Tanggal", "Gudang", "Jenis", "Keterangan", "Masuk", "Keluar", "Saldo")
        sheet.add("", "", "Saldo awal", "", "", "", card["opening"], fmt="#,##0")
        for r in card["rows"]:
            sheet.add(r["date"], r["warehouse"], r["move"].get_kind_display(), r["move"].reason, r["in"], r["out"], r["balance"], fmt="#,##0")
        sheet.finish("A5")
        return exports.response(wb, f"kartu-stok-{product.sku or product.pk}.xlsx")
    candidates = Product.objects.filter(store=store, name__icontains=query).order_by("name")[:8] if query and not product else []
    return render(request, "merchant/stock_card.html", _merchant_context(request, store, product=product, card=card, query=query,
        start=start, end=end, candidates=candidates))


@login_required(login_url=LOGIN)
def merchant_slow_moving(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        days = max(7, min(int(request.GET.get("days", "90")), 720))
    except ValueError:
        days = 90
    report = inventory_reports.slow_moving(store, days=days, today=timezone.localdate())
    return render(request, "merchant/slow_moving.html", _merchant_context(request, store, report=report, days=days))


@login_required(login_url=LOGIN)
def merchant_inventory_close(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    picture = inventory_reports.close_picture(store, timezone.localdate())
    return render(request, "merchant/inventory_close.html", _merchant_context(request, store, picture=picture))



@login_required(login_url=LOGIN)
def merchant_inventory_mutation(request):
    """Mutasi persediaan for a period: units per item and Persediaan in rupiah per kind of document."""
    from apps.catalog.models import Warehouse
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    start = _date(request.GET.get("start")) or today.replace(day=1)
    end = _date(request.GET.get("end")) or today
    if start > end:
        start, end = end, start
    warehouses = list(Warehouse.objects.filter(store=store).order_by("priority", "id"))
    warehouse = next((w for w in warehouses if str(w.pk) == request.GET.get("gudang")), None)
    if request.GET.get("format") == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        exports.inventory_mutation(wb, store, start, end, warehouse=warehouse, first=True)
        return exports.response(wb, f"mutasi-persediaan-{store.slug}-{start}-{end}.xlsx")
    report = inventory_reports.mutation(store, start, end, warehouse=warehouse)
    return render(request, "merchant/inventory_mutation.html", _merchant_context(request, store, report=report,
        start=start, end=end, warehouses=warehouses if len(warehouses) > 1 else [], warehouse=warehouse,
        rows=report["rows"][:500], query=request.GET.urlencode()))
