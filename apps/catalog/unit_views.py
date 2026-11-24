"""Satuan barang: each product's base unit and the larger units it is bought or sold in. Logic in catalog/units.py."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import units
from .models import Product, ProductUnit

LOGIN = "/login/"
EDIT_ROLES = permissions.roles("catalog.edit")
VIEW_ROLES = EDIT_ROLES | permissions.roles("purchase.order") | permissions.roles("stock.receive") | {"viewer"}


@login_required(login_url=LOGIN)
def merchant_units(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    products = Product.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action")
        product = products.filter(pk=request.POST.get("product") or 0).first() if str(
            request.POST.get("product", "")).isdigit() else None
        if product is None:
            raise Http404
        try:
            if action == "base":
                name = request.POST.get("base_unit", "").strip()[:20]
                if not name:
                    raise units.UnitError("Isi nama satuan dasar (mis. pcs, botol, kg).")
                product.base_unit = name
                product.save(update_fields=["base_unit"])
                messages.success(request, f"Satuan dasar {product.name}: {name}.")
            elif action == "save":
                unit = ProductUnit.objects.filter(product=product, pk=request.POST.get("unit") or 0).first() if str(
                    request.POST.get("unit", "")).isdigit() else None
                saved = units.save_unit(product, unit=unit, name=request.POST.get("name"),
                    factor=request.POST.get("factor") or 0, barcode=request.POST.get("barcode", ""))
                audit_log(store, request.user, "product_unit.saved", product, unit=saved.name, factor=saved.factor)
                messages.success(request, f"Satuan {saved} untuk {product.name} disimpan.")
            elif action == "delete":
                unit = ProductUnit.objects.filter(product=product, pk=request.POST.get("unit") or 0).first()
                if unit is not None:
                    unit.is_active = False   # documents that used it keep their factor; the unit leaves the lists
                    unit.save(update_fields=["is_active"])
                    audit_log(store, request.user, "product_unit.removed", product, unit=unit.name)
                    messages.success(request, f"Satuan {unit.name} tidak dipakai lagi untuk {product.name}.")
            else:
                raise Http404
        except (units.UnitError, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect(request.path + (f"?q={request.POST.get('q')}" if request.POST.get("q") else ""))
    query = request.GET.get("q", "").strip()
    rows = products.annotate(unit_count=Count("units", filter=Q(units__is_active=True))).prefetch_related("units")
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(sku__icontains=query))
    elif request.GET.get("show") != "all":
        rows = rows.filter(unit_count__gt=0)
    return render(request, "merchant/units.html", _merchant_context(request, store, rows=rows.order_by("name")[:300],
        query=query, show=request.GET.get("show", ""), all_products=products.filter(variants__isnull=True).order_by("name"),
        can_edit=permissions.can(request.user, store, "catalog.edit")))
