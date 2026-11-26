"""Impor data (every import on one page) and Bulan marketplace (a shop's month as five steps)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.permissions import roles

from . import import_hub, marketplace_month


@login_required(login_url="/login/")
def merchant_import_hub(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit") | roles("channels.manage") | roles("catalog.edit")
        | roles("customers.edit") | roles("books.edit"))
    return render(request, "merchant/import_hub.html", _merchant_context(request, store,
        cards=import_hub.cards(store, request.user)))


def _month(request):
    today = timezone.localdate()
    try:
        year, month = (int(part) for part in request.GET.get("month", "").split("-"))
        if 2000 <= year <= today.year and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    return today.year, today.month


@login_required(login_url="/login/")
def merchant_marketplace_month(request):
    from apps.billing.modules import full_books
    from apps.tax.services import month_bounds
    store = _staff_store(request)
    _require_roles(request, store, roles("channels.manage") | roles("channels.income") | roles("books.view"))
    year, month = _month(request)
    start, end = month_bounds(year, month)
    data = marketplace_month.build(store, start, end)
    if not full_books(store):   # Starter has no Tutup buku page: the step stays, without its button
        for shop in data["shops"]:
            for step in shop["steps"]:
                if step["key"] == "close":
                    step["url"] = ""
    return render(request, "merchant/marketplace_month.html", _merchant_context(request, store,
        month=f"{year}-{month:02d}", start=start, end=end, everything=full_books(store), **data))
