from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalog.models import Bundle, Product, ProductVariant
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.permissions import roles

from .mapping import MappingError, map_sku, resolve_unmapped, save_bundle
from .models import ChannelSku, SalesChannel, UnmappedSku

MAPPING_ROLES = roles("channels.mapping")


def _item(store, sku):
    """(product, variant) for a SKU of the store, or (None, None)."""
    sku = (sku or "").strip()
    if not sku:
        return None, None
    variant = ProductVariant.objects.select_related("product").filter(product__store=store,
        sku=sku).first()
    if variant:
        return variant.product, variant
    return Product.objects.filter(store=store, sku=sku).first(), None


@login_required(login_url="/login/")
def merchant_sku_map(request):
    """Pemetaan SKU marketplace ke katalog, dan antrean SKU yang belum dikenali."""
    store = _staff_store(request)
    _require_roles(request, store, MAPPING_ROLES)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "delete":
                get_object_or_404(ChannelSku, pk=request.POST.get("id") or 0,
                    channel__store=store).delete()
                messages.success(request, "Pemetaan dihapus.")
            else:
                target = request.POST.get("target", "").strip()
                product, variant = _item(store, target)
                bundle = Bundle.objects.filter(store=store, sku=target, is_active=True).first()
                if not product and not bundle:
                    raise MappingError(f"SKU {target or '(kosong)'} tidak ada di katalog atau paket.")
                queued = None
                if request.POST.get("unmapped"):
                    queued = get_object_or_404(UnmappedSku, pk=request.POST["unmapped"],
                        store=store)
                if queued:
                    _, moved = resolve_unmapped(queued, product=None if bundle else product,
                        variant=variant, bundle=bundle)
                    messages.success(request, f"SKU {queued.sku} dipetakan ke {target}"
                        + (f"; {moved} baris pesanan dipindahkan." if moved else "."))
                else:
                    channel = get_object_or_404(SalesChannel, pk=request.POST.get("channel") or 0,
                        store=store)
                    external = request.POST.get("external_sku", "").strip()
                    if not external:
                        raise MappingError("SKU marketplace wajib diisi.")
                    map_sku(channel, external, product=None if bundle else product,
                        variant=variant, bundle=bundle)
                    messages.success(request, f"SKU {external} dipetakan ke {target}.")
        except MappingError as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-sku-map")
    return render(request, "merchant/sku_map.html", _merchant_context(request, store,
        queue=UnmappedSku.objects.filter(store=store, resolved=False).select_related("channel"),
        mappings=ChannelSku.objects.filter(channel__store=store).select_related(
            "channel", "product", "variant", "bundle"),
        channels=SalesChannel.objects.filter(store=store, is_active=True)))


def _components(store, text):
    """Parse lines like ``KOPI x 2`` / ``KOPI:2`` / ``KOPI`` into ``[(product, variant, qty)]``."""
    components = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        sku, _, quantity = raw.replace("x", ":").replace("X", ":").replace("×", ":").partition(":")
        product, variant = _item(store, sku)
        if not product:
            raise MappingError(f"SKU {sku.strip()} tidak ada di katalog.")
        try:
            amount = int(quantity.strip() or 1)
        except ValueError:
            raise MappingError(f"Jumlah tidak valid pada baris: {raw}") from None
        if amount <= 0:
            raise MappingError(f"Jumlah harus lebih dari 0 pada baris: {raw}")
        components.append((product, variant, amount))
    return components


@login_required(login_url="/login/")
def merchant_bundles(request):
    """Paket: satu SKU jual yang terdiri dari beberapa barang stok."""
    store = _staff_store(request)
    _require_roles(request, store, MAPPING_ROLES)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "toggle":
                bundle = get_object_or_404(Bundle, pk=request.POST.get("id") or 0, store=store)
                bundle.is_active = not bundle.is_active
                bundle.save(update_fields=["is_active"])
            else:
                existing = None
                if request.POST.get("id"):
                    existing = get_object_or_404(Bundle, pk=request.POST["id"], store=store)
                save_bundle(store, sku=request.POST.get("sku", ""),
                    name=request.POST.get("name", ""), bundle=existing,
                    components=_components(store, request.POST.get("components", "")))
                messages.success(request, "Paket disimpan.")
        except MappingError as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-bundles")
    return render(request, "merchant/bundles.html", _merchant_context(request, store,
        bundles=Bundle.objects.filter(store=store).prefetch_related("items__product",
            "items__variant")))
