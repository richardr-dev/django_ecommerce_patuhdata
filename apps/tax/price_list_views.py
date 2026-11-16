"""Daftar harga: a customer group's prices. The list page creates one (dialog); the detail page
holds its rows (product → price) and its discount for everything else. Read by ``tax/pricing.py``."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounting.bank import parse_amount
from apps.catalog.models import PriceList, PriceListItem, Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog
from apps.shops.permissions import roles

LOGIN = "/login/"
VIEW_ROLES = roles("sales.view")
EDIT_ROLES = roles("sales.edit")


def _percent(text):
    try:
        value = Decimal(str(text or "0").replace(",", ".").strip() or "0")
    except InvalidOperation:
        raise ValueError("Persen diskon tidak valid.") from None
    if not Decimal(0) <= value <= Decimal(100):
        raise ValueError("Persen diskon harus antara 0 dan 100.")
    return value


@login_required(login_url=LOGIN)
def merchant_price_lists(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        name = request.POST.get("name", "").strip()[:120]
        try:
            if not name:
                raise ValueError("Nama daftar harga wajib diisi.")
            if PriceList.objects.filter(store=store, name__iexact=name).exists():
                raise ValueError(f"Daftar harga “{name}” sudah ada.")  # checked first: an IntegrityError would poison the transaction
            row = PriceList.objects.create(store=store, name=name, discount_percent=_percent(
                request.POST.get("discount_percent")), notes=request.POST.get("notes", "").strip())
        except ValueError as error:
            messages.error(request, str(error))
        else:
            AuditLog.objects.create(store=store, actor=request.user, action="price_list.created",
                object_type="PriceList", object_id=str(row.pk), details={"name": name})
            messages.success(request, f"Daftar harga {name} dibuat. Tambahkan harga per produk di bawah.")
            return redirect("merchant-price-list", list_id=row.pk)
        return redirect("merchant-price-lists")
    lists = PriceList.objects.filter(store=store).prefetch_related("items", "customers")
    return render(request, "merchant/price_lists.html", _merchant_context(request, store,
        lists=lists, can_edit=store.owner_id == request.user.id
            or store.staff_members.filter(user=request.user, role__in=EDIT_ROLES).exists()))


@login_required(login_url=LOGIN)
def merchant_price_list(request, list_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    price_list = get_object_or_404(PriceList, pk=list_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action", "")
        try:
            if action == "add":
                product = get_object_or_404(Product, pk=request.POST.get("product") or 0, store=store)
                price = parse_amount(request.POST.get("price", ""))
                if price is None or price < 0:
                    raise ValueError("Harga tidak valid.")
                PriceListItem.objects.update_or_create(price_list=price_list, product=product, variant=None,
                    defaults={"price": price})
                messages.success(request, f"Harga {product.name} disimpan.")
            elif action == "remove":
                PriceListItem.objects.filter(price_list=price_list, pk=request.POST.get("item") or 0).delete()
                messages.success(request, "Baris dihapus; produk itu kembali ke harga jual atau diskon daftar.")
            elif action == "update":
                name = request.POST.get("name", "").strip()[:120]
                if not name:
                    raise ValueError("Nama daftar harga wajib diisi.")
                if PriceList.objects.filter(store=store, name__iexact=name).exclude(pk=price_list.pk).exists():
                    raise ValueError(f"Daftar harga “{name}” sudah ada.")
                price_list.name = name
                price_list.discount_percent = _percent(request.POST.get("discount_percent"))
                price_list.is_active = bool(request.POST.get("is_active"))
                price_list.notes = request.POST.get("notes", "").strip()
                price_list.save()
                messages.success(request, "Daftar harga diperbarui.")
            else:
                raise Http404
        except ValueError as error:
            messages.error(request, str(error))
        return redirect("merchant-price-list", list_id=price_list.pk)
    items = price_list.items.select_related("product")
    listed = {item.product_id for item in items}
    products = Product.objects.filter(store=store, is_active=True).exclude(pk__in=listed).order_by("name")[:3000]
    return render(request, "merchant/price_list_detail.html", _merchant_context(request, store,
        price_list=price_list, items=items, products=products,
        customers=price_list.customers.order_by("name")[:50],
        can_edit=store.owner_id == request.user.id
            or store.staff_members.filter(user=request.user, role__in=EDIT_ROLES).exists()))
