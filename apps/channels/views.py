import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.billing.plans import channel_limit_error
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .adapters import PLATFORMS
from .forms import OrdersCsvForm, SalesChannelForm
from .models import SalesChannel
from .services import (
    CSV_COLUMNS,
    ChannelImportError,
    import_orders,
    match_listings,
    parse_orders_csv,
)

CHANNEL_ROLES = {"admin", "manager", "ops"}


@login_required(login_url="/merchant/login/")
def merchant_channels(request):
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})
    form = SalesChannelForm(request.POST or None)
    if request.method == "POST":
        _require_roles(request, store, CHANNEL_ROLES)
        limit_error = channel_limit_error(store, store.sales_channels.count() + 1)
        if limit_error:
            form.add_error(None, limit_error)
        elif form.is_valid():
            form.instance.store = store
            try:
                channel = form.save()
            except IntegrityError:
                form.add_error("name", "Toko marketplace ini sudah terhubung.")
            else:
                match_listings(channel)
                AuditLog.objects.create(store=store, actor=request.user, action="channel.created",
                    object_type="SalesChannel", object_id=str(channel.id),
                    details={"platform": channel.platform})
                messages.success(request, f"{channel} terhubung. SKU katalog sudah dicocokkan.")
                return redirect("merchant-channel-detail", channel_id=channel.id)
    channels = store.sales_channels.all()
    return render(request, "merchant/channels.html", _merchant_context(request, store,
        channels=channels, form=form, platforms=PLATFORMS))


@login_required(login_url="/merchant/login/")
def merchant_channel_detail(request, channel_id):
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})
    channel = get_object_or_404(SalesChannel, id=channel_id, store=store)
    csv_form, summary = OrdersCsvForm(), None
    if request.method == "POST":
        _require_roles(request, store, CHANNEL_ROLES)
        action = request.POST.get("action")
        if action == "match":
            added = match_listings(channel)
            messages.success(request, f"{added} SKU baru dicocokkan.")
            return redirect("merchant-channel-detail", channel_id=channel.id)
        if action == "toggle":
            channel.is_active = not channel.is_active
            channel.save(update_fields=["is_active"])
            return redirect("merchant-channel-detail", channel_id=channel.id)
        if action == "import":
            csv_form = OrdersCsvForm(request.POST, request.FILES)
            if csv_form.is_valid():
                try:
                    summary = import_orders(channel, parse_orders_csv(csv_form.cleaned_data["file"]))
                except (ChannelImportError, UnicodeDecodeError, csv.Error) as error:
                    csv_form.add_error("file", f"File tidak dapat dibaca: {error}")
                else:
                    AuditLog.objects.create(store=store, actor=request.user,
                        action="channel.orders_imported", object_type="SalesChannel",
                        object_id=str(channel.id), details={"created": summary["created"],
                            "duplicates": summary["duplicates"], "errors": len(summary["errors"])})
        else:
            raise Http404
    listings = channel.listings.select_related("product", "variant")[:500]
    imported = channel.orders.select_related("order").order_by("-imported_at")[:50]
    return render(request, "merchant/channel_detail.html", _merchant_context(request, store,
        channel=channel, listings=listings, imported=imported, csv_form=csv_form,
        summary=summary, platform=PLATFORMS[channel.platform]))


@login_required(login_url="/merchant/login/")
def channel_orders_template(request):
    _staff_store(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="template-pesanan-marketplace.csv"'
    writer = csv.writer(response)
    writer.writerow(CSV_COLUMNS)
    writer.writerow(["SHP-240001", "Budi Santoso", "081234567890", "Jl. Merdeka 1, Bandung 40111",
        "SKU-001", "2", "75000", "12000", "5000", "J&T Express", "JT1234567890"])
    writer.writerow(["SHP-240001", "", "", "", "SKU-002", "1", "50000", "", "", "", ""])
    return response


@login_required(login_url="/merchant/login/")
def channel_stock_csv(request, channel_id):
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})
    channel = get_object_or_404(SalesChannel, id=channel_id, store=store)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="stok-{channel.platform}-{store.slug}.csv"'
    writer = csv.writer(response)
    writer.writerow(["sku", "produk", "varian", "stok_tersedia"])
    for listing in channel.listings.select_related("product", "variant"):
        writer.writerow([listing.sku, listing.product.name,
            listing.variant.name if listing.variant_id else "", listing.available_stock])
    return response
