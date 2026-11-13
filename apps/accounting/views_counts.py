"""Stok opname documents: open, enter, approve."""
import datetime
import uuid as _uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalog.models import Warehouse
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import stock_counts
from .ledger import entries_for
from .models import StockCount

LOGIN = "/merchant/login/"
COUNT_ROLES = permissions.roles("stock.count") | permissions.roles("stock.count_approve")
VIEW_ROLES = COUNT_ROLES | {"viewer"}


@login_required(login_url=LOGIN)
def merchant_stock_counts(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, permissions.roles("stock.count"))
        warehouse = Warehouse.objects.filter(store=store, pk=request.POST.get("warehouse") or 0).first()
        try:
            if not warehouse:
                raise stock_counts.CountError("Pilih gudang yang akan dihitung.")
            count = stock_counts.open_count(store, warehouse=warehouse, actor=request.user, notes=request.POST.get("notes", ""),
                count_date=datetime.date.fromisoformat(request.POST.get("count_date") or timezone.localdate().isoformat()))
        except (stock_counts.CountError, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        else:
            audit_log(store, request.user, "stock_count.opened", count, warehouse=warehouse.name)
            return redirect("merchant-stock-count", count_id=count.pk)
        return redirect("merchant-stock-counts")
    return render(request, "merchant/stock_counts.html", _merchant_context(request, store,
        rows=StockCount.objects.filter(store=store).select_related("warehouse", "created_by")[:200],
        warehouses=Warehouse.objects.filter(store=store, is_active=True), today=timezone.localdate().isoformat(),
        can_count=permissions.can(request.user, store, "stock.count")))


@login_required(login_url=LOGIN)
def merchant_stock_count(request, count_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        count = get_object_or_404(StockCount.objects.select_related("warehouse", "created_by", "approved_by"),
            pk=_uuid.UUID(str(count_id)), store=store)
    except ValueError:
        raise Http404 from None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("save", "approve"):
                _require_roles(request, store, permissions.roles("stock.count"))
                counted = {}
                for line in count.lines.all():
                    raw = request.POST.get(f"counted_{line.pk}", "").strip()
                    counted[line.pk] = int(raw) if raw else None
                stock_counts.enter_counts(count, counted)
            if action == "approve":
                stock_counts.approve(count, actor=request.user)
                audit_log(store, request.user, "stock_count.approved", count, value=stock_counts.summary(count)["value"])
                messages.success(request, "Stok opname disetujui. Stok dan pembukuan sudah disesuaikan.")
            elif action == "save":
                messages.success(request, "Hasil hitung disimpan.")
            elif action == "cancel":
                _require_roles(request, store, permissions.roles("stock.count"))
                stock_counts.cancel(count)
                audit_log(store, request.user, "stock_count.cancelled", count)
                messages.success(request, "Stok opname dibatalkan.")
        except (stock_counts.CountError, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-stock-count", count_id=count.pk)
    lines = list(count.lines.select_related("product"))
    return render(request, "merchant/stock_count_detail.html", _merchant_context(request, store, count=count, lines=lines,
        summary=stock_counts.summary(count), journal=entries_for(store, [("stock_count", count.pk)]),
        audit=audit_trail(store, count), editable=count.status == StockCount.DRAFT,
        can_count=permissions.can(request.user, store, "stock.count"),
        can_approve=permissions.can(request.user, store, "stock.count_approve")))
