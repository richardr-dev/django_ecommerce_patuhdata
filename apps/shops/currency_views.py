"""Pengaturan › Mata uang: the company's book currency (apps/shops/currency.py)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.orders.views import _merchant_context, _require_roles, _staff_store

from . import currency
from .permissions import roles


@login_required(login_url="/login/")
def merchant_currency_settings(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("books.view") | roles("settings.currency"))
    if request.method == "POST":
        _require_roles(request, store, roles("settings.currency"))
        try:
            currency.set_currency(store, request.POST.get("currency", ""), actor=request.user)
        except currency.CurrencyError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"Mata uang pembukuan: {currency.for_store(store).label}.")
        return redirect("merchant-currency-settings")
    allowed, reason = currency.can_change(store)
    active = currency.for_store(store)
    options = [{"code": c.code, "label": c.label, "symbol": c.symbol, "enabled": c.code in currency.ENABLED,
        "example": c.format(1250000.5), "current": c.code == active.code} for c in currency.CURRENCIES.values()]
    return render(request, "merchant/currency_settings.html", _merchant_context(request, store,
        title="Mata uang", options=options, active=active, active_example=active.format(1250000), can_change=allowed, locked_reason=reason,
        can_edit=request.user == store.owner or store.staff_members.filter(user=request.user,
            role__in=roles("settings.currency")).exists()))
