"""Lot & kedaluwarsa: lots held with their expiry dates, which products track lots, and where a lot went.
Logic lives in orders/lots.py."""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalog.models import Product
from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import lots
from .models import StockLot
from .views import _merchant_context, _require_roles, _staff_store

LOGIN = "/login/"
EDIT_ROLES = permissions.roles("catalog.edit")
VIEW_ROLES = EDIT_ROLES | permissions.roles("stock.receive") | permissions.roles("stock.fulfil") | {"viewer"}


@login_required(login_url=LOGIN)
def merchant_lots(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        product = Product.objects.filter(store=store, pk=request.POST.get("product") or 0).first() if str(
            request.POST.get("product", "")).isdigit() else None
        if product is None:
            raise Http404
        if request.POST.get("action") == "track":
            lots.start_tracking(product, actor=request.user)
            audit_log(store, request.user, "product.lots_on", product)
            messages.success(request, f"Lot & kedaluwarsa {product.name} dilacak. Stok yang ada masuk lot AWAL; "
                "isi lot dan tanggal kedaluwarsa saat menerima barang berikutnya.")
        elif request.POST.get("action") == "untrack":
            lots.stop_tracking(product)
            audit_log(store, request.user, "product.lots_off", product)
            messages.success(request, f"Lot {product.name} tidak dilacak lagi.")
        else:
            raise Http404
        return redirect(request.path + "?tab=produk")
    today = timezone.localdate()
    status = request.GET.get("status", "")
    rows = StockLot.objects.filter(store=store, quantity__gt=0, product__tracks_lots=True).select_related(
        "product", "variant")
    if status == "expired":
        rows = rows.filter(expiry_date__lt=today)
    elif status == "soon":
        rows = rows.filter(expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=30))
    query = request.GET.get("q", "").strip()
    if query:
        rows = rows.filter(Q(code__icontains=query) | Q(product__name__icontains=query) | Q(product__sku__icontains=query))
    rows = list(rows.order_by("expiry_date", "received_on")[:500])
    for lot in rows:
        lot.days_left = (lot.expiry_date - today).days if lot.expiry_date else None
    due = lots.expiring(store, today=today)
    return render(request, "merchant/lots.html", _merchant_context(request, store, rows=rows, status=status, query=query,
        tab=request.GET.get("tab", "lot"), expired=len(due["expired"]), soon=len(due["soon"]),
        products=Product.objects.filter(store=store, is_active=True, is_digital=False).order_by("-tracks_lots", "name")[:500],
        mismatches=lots.mismatches(store), can_edit=permissions.can(request.user, store, "catalog.edit")))


@login_required(login_url=LOGIN)
def merchant_lot(request, lot_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    lot = get_object_or_404(StockLot.objects.select_related("product", "variant"), pk=lot_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        raw = request.POST.get("expiry_date", "")
        try:
            lots.set_expiry(lot, datetime.date.fromisoformat(raw) if raw else None)
        except ValueError:
            messages.error(request, "Tanggal kedaluwarsa tidak terbaca.")
        else:
            audit_log(store, request.user, "lot.expiry_set", lot, code=lot.code, expiry=raw)
            messages.success(request, f"Tanggal kedaluwarsa lot {lot.code} disimpan.")
        return redirect("merchant-lot", lot_id=lot.pk)
    return render(request, "merchant/lot_detail.html", _merchant_context(request, store, lot=lot,
        moves=lots.trace(lot), today=timezone.localdate(), can_edit=permissions.can(request.user, store, "catalog.edit")))
