"""Stok minus: the store's switch and the list of sales still waiting for goods."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import deficits
from .views import _merchant_context, _require_roles, _staff_store


@login_required(login_url="/merchant/login/")
def merchant_stock_minus(request):
    store = _staff_store(request)
    _require_roles(request, store, permissions.roles("stock.fulfil") | permissions.roles("books.view"))
    can_switch = permissions.can(request.user, store, "settings.manage")
    if request.method == "POST":
        if not can_switch:
            messages.error(request, "Hanya admin atau manajer yang bisa mengubah pengaturan ini.")
            return redirect("merchant-stock-minus")
        allow = request.POST.get("allow") == "1"
        if not allow and deficits.summary(store)["units"]:
            messages.info(request, "Stok minus dimatikan untuk penjualan baru. Yang masih terbuka tetap "
                "ditutup saat barangnya diterima.")
        store.allow_negative_stock = allow
        store.save(update_fields=["allow_negative_stock"])
        audit_log(store, request.user, "stock.negative_allowed" if allow else "stock.negative_refused", store)
        messages.success(request, "Stok minus diizinkan." if allow else "Stok minus tidak diizinkan lagi.")
        return redirect("merchant-stock-minus")
    rows = list(deficits.open_deficits(store).select_related("product", "order_item__order", "created_by"))
    for row in rows:
        row.value = row.open * row.provisional_cost
    recent = (store.stock_deficits.filter(covered__gt=0).select_related("product").prefetch_related("covers")
        .order_by("-id")[:20])
    return render(request, "merchant/stock_minus.html", _merchant_context(request, store, rows=rows,
        summary=deficits.summary(store), recent=recent, can_switch=can_switch,
        allowed=store.allow_negative_stock))
