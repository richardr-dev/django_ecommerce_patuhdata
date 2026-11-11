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
from .forms import OrdersImportForm, SalesChannelForm
from apps.catalog.views import paginate

from .models import ChannelBuyer, ImportBatch, SalesChannel
from .services import (
    CSV_COLUMNS,
    ChannelImportError,
    get_or_create_channel,
    import_orders,
    match_listings,
    parse_orders_csv,
)

# Keep old alias used in a few tests.
OrdersCsvForm = OrdersImportForm

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
    import_form, summary = OrdersImportForm(), None
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
            import_form = OrdersImportForm(request.POST, request.FILES)
            if import_form.is_valid():
                upload = import_form.cleaned_data["file"]
                try:
                    skipped = 0
                    if upload.name.lower().endswith(".xlsx"):
                        from .parsers.shopee import parse_shopee_xlsx
                        orders, skipped = parse_shopee_xlsx(upload)
                    else:
                        orders = parse_orders_csv(upload)
                    summary = import_orders(channel, orders)
                    summary["skipped"] = skipped
                except (ChannelImportError, UnicodeDecodeError, csv.Error) as error:
                    import_form.add_error("file", f"File tidak dapat dibaca: {error}")
                else:
                    AuditLog.objects.create(store=store, actor=request.user,
                        action="channel.orders_imported", object_type="SalesChannel",
                        object_id=str(channel.id), details={"created": summary["created"],
                            "duplicates": summary["duplicates"],
                            "skipped": summary.get("skipped", 0),
                            "errors": len(summary["errors"])})
        else:
            raise Http404
    listings = channel.listings.select_related("product", "variant")[:500]
    imported = channel.orders.select_related("order").order_by("-imported_at")[:50]
    return render(request, "merchant/channel_detail.html", _merchant_context(request, store,
        channel=channel, listings=listings, imported=imported,
        csv_form=import_form,  # template still uses csv_form variable name
        import_form=import_form,
        summary=summary, platform=PLATFORMS[channel.platform]))


@login_required(login_url="/merchant/login/")
def merchant_import(request):
    """3-step import wizard: pick platform → upload file → results saved to ImportBatch."""
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)

    import csv as _csv

    # Step metadata per platform
    IMPORT_PLATFORMS = {
        "shopee": {
            "label": "Shopee", "icon": "🟠", "file_type": "xlsx",
            "color": "#EE4D2D", "text_color": "#fff",
            "tagline": "Ekspor langsung dari Seller Center",
            "badge": ".xlsx",
            "instructions": [
                "Buka Shopee Seller Center → Pesanan Saya",
                "Klik Ekspor Pesanan, pilih rentang tanggal",
                "Unduh file .xlsx yang dikirim ke email",
                "Upload file tersebut di sini — tidak perlu reformatting",
            ],
            "accept": ".xlsx",
        },
        "tokopedia": {
            "label": "Tokopedia", "icon": "🟢", "file_type": "xlsx",
            "color": "#42B549", "text_color": "#fff",
            "tagline": "Ekspor langsung dari Seller Center",
            "badge": ".xlsx",
            "instructions": [
                "Buka Tokopedia Seller Center → Pesanan → Daftar Pesanan",
                "Klik Ekspor, pilih rentang tanggal dan status pesanan",
                "Unduh file .xlsx yang dihasilkan (bukan template CSV)",
                "Upload langsung di sini — tidak perlu reformatting",
            ],
            "accept": ".xlsx,.csv",
        },
        "tiktok_shop": {
            "label": "TikTok Shop", "icon": "⬛", "file_type": "csv",
            "color": "#010101", "text_color": "#fff",
            "tagline": "Via template CSV Lakuin",
            "badge": ".csv",
            "instructions": [
                "Unduh template CSV Lakuin",
                "Buka TikTok Seller Center → Pesanan → Ekspor",
                "Salin data ke template CSV, upload di sini",
            ],
            "accept": ".csv",
        },
        "lazada": {
            "label": "Lazada", "icon": "🔵", "file_type": "csv",
            "color": "#0F146D", "text_color": "#fff",
            "tagline": "Via template CSV Lakuin",
            "badge": ".csv",
            "instructions": [
                "Unduh template CSV Lakuin",
                "Buka Lazada Seller Center → Pesanan → Export",
                "Salin data ke template CSV, upload di sini",
            ],
            "accept": ".csv",
        },
        "manual": {
            "label": "Manual / Lainnya", "icon": "📋", "file_type": "csv",
            "color": "#6B7280", "text_color": "#fff",
            "tagline": "Channel lain atau input manual",
            "badge": ".csv",
            "instructions": [
                "Unduh template CSV Lakuin",
                "Isi data pesanan dari channel manapun",
                "Upload file .csv di sini",
            ],
            "accept": ".csv",
        },
    }

    platform = request.GET.get("platform") or request.POST.get("platform", "")
    selected = IMPORT_PLATFORMS.get(platform)
    form = OrdersImportForm()
    summary = None

    if request.method == "POST" and selected:
        form = OrdersImportForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["file"]
            soft = request.POST.get("soft") == "1"
            try:
                skipped = 0
                is_xlsx = upload.name.lower().endswith(".xlsx")
                if is_xlsx and platform == "shopee":
                    from .parsers.shopee import parse_shopee_xlsx
                    orders, skipped = parse_shopee_xlsx(upload)
                elif is_xlsx and platform == "tokopedia":
                    from .parsers.tokopedia import parse_tokopedia_xlsx
                    orders, skipped = parse_tokopedia_xlsx(upload)
                elif is_xlsx:
                    # Unknown platform XLSX: try Shopee then Tokopedia format
                    try:
                        from .parsers.shopee import parse_shopee_xlsx
                        orders, skipped = parse_shopee_xlsx(upload)
                    except ChannelImportError:
                        upload.seek(0)
                        from .parsers.tokopedia import parse_tokopedia_xlsx
                        orders, skipped = parse_tokopedia_xlsx(upload)
                else:
                    orders = parse_orders_csv(upload)

                channel = get_or_create_channel(store, platform,
                    name=f"Toko {selected['label']}")
                category = request.POST.get("category", "regular")
                summary = import_orders(channel, orders, soft=soft,
                    file_name=upload.name, imported_by=request.user, skipped=skipped,
                    category=category)
                summary["skipped"] = skipped
                summary["platform_label"] = selected["label"]

                from apps.shops.models import AuditLog
                AuditLog.objects.create(store=store, actor=request.user,
                    action="channel.orders_imported", object_type="SalesChannel",
                    object_id=str(channel.id),
                    details={"platform": platform, "created": summary["created"],
                             "duplicates": summary["duplicates"], "skipped": skipped,
                             "soft": soft, "errors": len(summary["errors"])})
                if summary["created"]:
                    messages.success(request,
                        f"{summary['created']} pesanan dari {selected['label']} berhasil diimpor.")
            except (ChannelImportError, UnicodeDecodeError, _csv.Error) as error:
                form.add_error("file", f"File tidak dapat dibaca: {error}")

    return render(request, "merchant/import.html", _merchant_context(request, store,
        import_platforms=IMPORT_PLATFORMS,
        platform=platform, selected=selected,
        form=form, summary=summary,
    ))


@login_required(login_url="/merchant/login/")
def merchant_sales(request):
    """Unified sales dashboard — all channels combined: Lakuin, Shopee, Tokopedia, etc."""
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    from decimal import Decimal
    from datetime import date, timedelta
    from django.db.models import Count, Sum, Avg
    from django.db.models.functions import TruncDate, TruncMonth
    from django.utils import timezone
    from apps.orders.models import Order, OrderItem

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking",
                        "packed", "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    # --- Date range filter ---
    today = timezone.localdate()
    date_from_raw = request.GET.get("date_from", "")
    date_to_raw = request.GET.get("date_to", "")
    try:
        date_from = date.fromisoformat(date_from_raw) if date_from_raw else today - timedelta(days=29)
    except ValueError:
        date_from = today - timedelta(days=29)
    try:
        date_to = date.fromisoformat(date_to_raw) if date_to_raw else today
    except ValueError:
        date_to = today

    channel_filter = request.GET.get("channel", "")

    base_qs = Order.objects.filter(store=store, status__in=REVENUE_STATUSES)
    if channel_filter:
        base_qs = base_qs.filter(channel=channel_filter)

    period_qs = base_qs.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)

    # --- Summary cards ---
    totals = period_qs.aggregate(
        revenue=Sum("total"), orders=Count("id"), customers=Count("phone", distinct=True),
        discount=Sum("discount"), shipping=Sum("shipping_cost"),
    )
    revenue = totals["revenue"] or Decimal("0")
    order_count = totals["orders"] or 0
    aov = revenue / order_count if order_count else Decimal("0")

    # --- Channel breakdown ---
    channel_rows = (period_qs
        .values("channel")
        .annotate(revenue=Sum("total"), orders=Count("id"),
                  customers=Count("phone", distinct=True))
        .order_by("-revenue"))
    channel_label_map = dict(Order.CHANNELS)
    for row in channel_rows:
        row["label"] = channel_label_map.get(row["channel"], row["channel"])
        row["pct"] = round(float(row["revenue"] / revenue * 100), 1) if revenue else 0

    # --- Daily trend (bar chart data) ---
    daily = list(period_qs
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("day"))
    peak = max((r["revenue"] for r in daily), default=Decimal("0"))
    for row in daily:
        row["pct"] = round(float(row["revenue"] / peak * 100), 1) if peak else 0

    # --- Monthly trend ---
    monthly = list(base_qs
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("-month")[:12])
    monthly_peak = max((r["revenue"] for r in monthly), default=Decimal("0"))
    for row in monthly:
        row["pct"] = round(float(row["revenue"] / monthly_peak * 100), 1) if monthly_peak else 0

    # --- Top products ---
    top_products = list(
        OrderItem.objects.filter(order__in=period_qs)
        .values("name")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"))
        .order_by("-revenue")[:10])

    # --- Import batches in period ---
    recent_batches = (ImportBatch.objects
        .filter(store=store, imported_at__date__gte=date_from, imported_at__date__lte=date_to)
        .select_related("channel")
        .order_by("-imported_at")[:10])

    # --- Recent orders ---
    recent_orders = (Order.objects.filter(store=store)
        .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        .order_by("-created_at")
        .prefetch_related("items")[:15])

    # --- Channel choices for filter ---
    channel_choices = [(ch, channel_label_map.get(ch, ch)) for ch in
                       Order.objects.filter(store=store)
                       .values_list("channel", flat=True).distinct().order_by("channel")]

    return render(request, "merchant/sales.html", _merchant_context(request, store,
        # Filters
        date_from=date_from, date_to=date_to,
        channel_filter=channel_filter, channel_choices=channel_choices,
        # Summary
        revenue=revenue, order_count=order_count, aov=aov,
        unique_customers=totals["customers"] or 0,
        total_discount=totals["discount"] or Decimal("0"),
        # Charts
        channel_rows=list(channel_rows),
        daily=daily, monthly=monthly,
        # Lists
        top_products=top_products,
        recent_batches=recent_batches,
        recent_orders=recent_orders,
    ))


@login_required(login_url="/merchant/login/")
def merchant_sales_stats(request):
    """htmx partial — summary cards + channel breakdown for the sales dashboard."""
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    from decimal import Decimal
    from datetime import date, timedelta
    from django.db.models import Count, Sum
    from django.utils import timezone
    from apps.orders.models import Order

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking",
                        "packed", "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    today = timezone.localdate()
    date_from_raw = request.GET.get("date_from", "")
    date_to_raw = request.GET.get("date_to", "")
    try:
        date_from = date.fromisoformat(date_from_raw) if date_from_raw else today - timedelta(days=29)
    except ValueError:
        date_from = today - timedelta(days=29)
    try:
        date_to = date.fromisoformat(date_to_raw) if date_to_raw else today
    except ValueError:
        date_to = today

    channel_filter = request.GET.get("channel", "")
    base_qs = Order.objects.filter(store=store, status__in=REVENUE_STATUSES)
    if channel_filter:
        base_qs = base_qs.filter(channel=channel_filter)
    period_qs = base_qs.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)

    totals = period_qs.aggregate(
        revenue=Sum("total"), orders=Count("id"), customers=Count("phone", distinct=True),
        discount=Sum("discount"),
    )
    revenue = totals["revenue"] or Decimal("0")
    order_count = totals["orders"] or 0
    aov = revenue / order_count if order_count else Decimal("0")

    channel_rows = (period_qs
        .values("channel")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("-revenue"))
    channel_label_map = dict(Order.CHANNELS)
    for row in channel_rows:
        row["label"] = channel_label_map.get(row["channel"], row["channel"])
        row["pct"] = round(float(row["revenue"] / revenue * 100), 1) if revenue else 0

    return render(request, "merchant/sales_stats_partial.html", {
        "revenue": revenue, "order_count": order_count, "aov": aov,
        "unique_customers": totals["customers"] or 0,
        "total_discount": totals["discount"] or Decimal("0"),
        "channel_rows": list(channel_rows),
    })


@login_required(login_url="/merchant/login/")
def merchant_sales_invoices(request):
    """Unified sales invoice list — all orders from all channels."""
    from decimal import Decimal
    from django.db.models import Count, Sum
    from django.utils.dateparse import parse_date as _parse_date
    from apps.orders.models import Order

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    # Bulk issue invoices for all uninvoiced orders
    if request.method == "POST":
        _require_roles(request, store, CHANNEL_ROLES)
        if request.POST.get("action") == "bulk_issue":
            from apps.tax.services import issue_invoice_for_order
            from apps.orders.models import Order as _Ord
            count = 0
            for _o in _Ord.objects.filter(store=store, invoice__isnull=True):
                try:
                    _, created = issue_invoice_for_order(_o)
                    if created:
                        count += 1
                except Exception:
                    pass
            messages.success(request, f"{count} invoice berhasil diterbitkan.")
            return redirect("merchant-sales-invoices")

    # Mark overdue invoices at view time
    import datetime as _dt
    from apps.tax.models import Invoice as _Inv
    _Inv.objects.filter(
        store=store, status=_Inv.STATUS_ISSUED,
        due_date__lt=_dt.date.today(),
    ).update(status=_Inv.STATUS_OVERDUE)

    from django.db.models import Count as _Count, Sum as _Sum
    invoice_stats = list(_Inv.objects.filter(store=store).values("status").annotate(
        count=_Count("id"), total=_Sum("total")).order_by("status"))

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed",
                        "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    # --- Filters ---
    channel_filter = request.GET.get("channel", "")
    status_filter  = request.GET.get("status", "")
    month_filter   = request.GET.get("month", "")
    date_from_raw  = request.GET.get("date_from", "")
    date_to_raw    = request.GET.get("date_to", "")
    q              = request.GET.get("q", "").strip()

    qs = Order.objects.filter(store=store).select_related("invoice", "customer").order_by("-created_at")

    if channel_filter:
        qs = qs.filter(channel=channel_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if month_filter:
        try:
            d = _parse_date(month_filter + "-01")
            if d:
                qs = qs.filter(created_at__year=d.year, created_at__month=d.month)
        except (ValueError, AttributeError):
            pass
    elif date_from_raw or date_to_raw:
        try:
            df = _parse_date(date_from_raw) if date_from_raw else None
            dt = _parse_date(date_to_raw) if date_to_raw else None
            if df:
                qs = qs.filter(created_at__date__gte=df)
            if dt:
                qs = qs.filter(created_at__date__lte=dt)
        except (ValueError, AttributeError):
            pass

    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(customer_name__icontains=q) |
            Q(invoice_number__icontains=q) |
            Q(external_order_id__icontains=q)
        )

    # --- Summary cards (over filtered qs) ---
    rev_qs = qs.filter(status__in=REVENUE_STATUSES)
    totals = rev_qs.aggregate(
        revenue=Sum("total"), orders=Count("id"),
        discount=Sum("discount"), shipping=Sum("shipping_cost"),
    )
    revenue      = totals["revenue"]  or Decimal("0")
    order_count  = totals["orders"]   or 0
    total_disc   = totals["discount"] or Decimal("0")

    # Channel breakdown (revenue orders only)
    channel_label_map = dict(Order.CHANNELS)
    channel_rows = list(rev_qs
        .values("channel")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("-revenue"))
    for row in channel_rows:
        row["label"] = channel_label_map.get(row["channel"], row["channel"])
        row["pct"]   = round(float(row["revenue"] / revenue * 100), 1) if revenue else 0

    # --- Available filter choices ---
    used_channels = (Order.objects.filter(store=store)
                     .values_list("channel", flat=True).distinct().order_by("channel"))
    channel_choices = [(c, channel_label_map.get(c, c)) for c in used_channels]

    used_statuses = (Order.objects.filter(store=store)
                     .values_list("status", flat=True).distinct().order_by("status"))
    status_label_map = dict(Order._meta.get_field("status").choices) if hasattr(
        Order._meta.get_field("status"), "choices") else {}
    status_choices = [(s, status_label_map.get(s, s)) for s in used_statuses]

    # Recent import batches (sidebar context)
    recent_batches = (ImportBatch.objects.filter(store=store)
                      .select_related("channel").order_by("-imported_at")[:5])

    aov = revenue / order_count if order_count else Decimal("0")

    # Build channel tabs: (code_or_empty, label, count)
    channel_label_map = dict(Order.CHANNELS)
    all_count = Order.objects.filter(store=store).count()
    channel_tabs = [("", "Semua", all_count)]
    for ch_code, ch_label in Order.CHANNELS:
        cnt = Order.objects.filter(store=store, channel=ch_code).count()
        if cnt:
            channel_tabs.append((ch_code, ch_label, cnt))

    page, page_range, page_query = paginate(request, qs, 50)

    return render(request, "merchant/sales_invoices.html", _merchant_context(request, store,
        orders=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        channel_filter=channel_filter, status_filter=status_filter,
        month_filter=month_filter, date_from=date_from_raw, date_to=date_to_raw,
        query=q,
        channel_choices=channel_choices, status_choices=status_choices,
        revenue=revenue, order_count=order_count, total_disc=total_disc, aov=aov,
        channel_rows=channel_rows, channel_tabs=channel_tabs,
        recent_batches=recent_batches,
        invoice_stats=invoice_stats,
    ))


@login_required(login_url="/merchant/login/")
def merchant_channel_buyers(request):
    """Marketplace buyer profiles auto-created from XLSX/CSV imports."""
    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    qs = ChannelBuyer.objects.filter(store=store)

    platform_filter = request.GET.get("platform", "")
    if platform_filter:
        qs = qs.filter(platform=platform_filter)

    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(name__icontains=q) | Q(external_id__icontains=q) |
            Q(city__icontains=q) | Q(province__icontains=q)
        )

    member_filter = request.GET.get("member", "")
    if member_filter == "1":
        qs = qs.filter(is_member=True)

    page, page_range, page_query = paginate(request, qs, 25)

    return render(request, "merchant/channel_buyers.html", _merchant_context(request, store,
        buyers=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        platforms=ChannelBuyer.PLATFORMS, platform_filter=platform_filter,
        query=q, member_filter=member_filter,
        total_buyers=ChannelBuyer.objects.filter(store=store).count(),
        member_count=ChannelBuyer.objects.filter(store=store, is_member=True).count(),
    ))


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


@login_required(login_url="/merchant/login/")
def merchant_invoice_new(request):
    """Create a manual sales invoice for any channel."""
    from decimal import Decimal, InvalidOperation
    from apps.catalog.models import Product
    from apps.orders.models import Order, OrderItem, OrderStatusEvent

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)

    products = Product.objects.filter(store=store, is_active=True).order_by("name")
    CHANNEL_CHOICES = [c for c in Order.CHANNELS if c[0] not in ("pos",)]

    if request.method == "POST":
        try:
            from django.db import transaction as _tx
            channel      = request.POST.get("channel", "manual")
            inv_date     = request.POST.get("invoice_date", "")
            cust_name    = request.POST.get("customer_name", "")[:120] or "Pelanggan"
            company_name = request.POST.get("company_name", "")[:160]
            buyer_npwp   = request.POST.get("buyer_npwp", "")[:20]
            phone        = request.POST.get("phone", "-")[:30] or "-"
            address      = request.POST.get("address", "-")[:300] or "-"
            notes        = request.POST.get("notes", "")[:300]
            ext_id       = request.POST.get("external_order_id", "")[:100]
            customer_id  = request.POST.get("customer_id", "")

            # Link to existing CRM customer if provided
            crm_customer = None
            if customer_id:
                from apps.shops.models import Customer as _Cust
                try:
                    crm_customer = _Cust.objects.get(id=customer_id, store=store)
                    # Enrich customer with any new data provided in this invoice
                    update_fields = []
                    if address and address != "-" and not crm_customer.address:
                        crm_customer.address = address; update_fields.append("address")
                    if buyer_npwp and not crm_customer.npwp:
                        crm_customer.npwp = buyer_npwp; update_fields.append("npwp")
                    if company_name and not crm_customer.company_name:
                        crm_customer.company_name = company_name; update_fields.append("company_name")
                    if update_fields:
                        crm_customer.save(update_fields=update_fields)
                except Exception:
                    crm_customer = None

            skus   = request.POST.getlist("sku[]")
            qtys   = request.POST.getlist("qty[]")
            prices = request.POST.getlist("unit_price[]")

            lines, subtotal = [], Decimal("0")
            errors = []
            for sku, qty_str, price_str in zip(skus, qtys, prices):
                sku = sku.strip()
                if not sku:
                    continue
                try:
                    qty        = int(qty_str)
                    unit_price = Decimal(price_str.replace(",", ""))
                except (ValueError, InvalidOperation):
                    continue
                if qty <= 0:
                    continue
                product = (Product.objects.filter(store=store, sku=sku).first()
                           or Product.objects.filter(store=store, name__iexact=sku).first())
                if not product:
                    errors.append(f"Produk '{sku}' tidak ditemukan.")
                    continue
                lt = unit_price * qty
                subtotal += lt
                lines.append({"product": product, "sku": product.sku, "name": product.name,
                               "qty": qty, "unit_price": unit_price, "line_total": lt})

            if errors:
                from django.contrib import messages as _msg
                for e in errors:
                    _msg.error(request, e)
                return render(request, "merchant/invoice_new.html",
                    _merchant_context(request, store, products=products,
                                      channel_choices=CHANNEL_CHOICES,
                                      today=__import__("datetime").date.today().isoformat()))
            if not lines:
                from django.contrib import messages as _msg
                _msg.error(request, "Tambahkan minimal satu produk.")
                return render(request, "merchant/invoice_new.html",
                    _merchant_context(request, store, products=products,
                                      channel_choices=CHANNEL_CHOICES,
                                      today=__import__("datetime").date.today().isoformat()))

            discount = Decimal("0")
            shipping = Decimal("0")
            try:
                discount = Decimal(request.POST.get("discount", "0").replace(",", ""))
                shipping = Decimal(request.POST.get("shipping", "0").replace(",", ""))
            except InvalidOperation:
                pass
            total = max(subtotal - discount, Decimal("0")) + shipping

            import uuid as _uuid_mod
            with _tx.atomic():
                order = Order.objects.create(
                    store=store, channel=channel,
                    customer_name=cust_name, phone=phone,
                    shipping_address=address,
                    subtotal=subtotal, discount=discount,
                    shipping_cost=shipping, total=total,
                    payment_method="manual", delivery_notes=notes,
                    external_order_id=ext_id,
                    customer=crm_customer,
                    idempotency_key=_uuid_mod.uuid4().hex,
                )
                if inv_date:
                    from django.utils.dateparse import parse_date as _pd
                    d = _pd(inv_date)
                    if d:
                        Order.objects.filter(pk=order.pk).update(created_at=d)
                        order.refresh_from_db()
                order.save()  # triggers invoice_number generation
                for line in lines:
                    OrderItem.objects.create(
                        order=order, product=line["product"],
                        name=line["name"], sku=line["sku"],
                        unit_price=line["unit_price"], quantity=line["qty"],
                        line_total=line["line_total"],
                    )
                OrderStatusEvent.objects.create(order=order, status="paid",
                    note=f"Invoice dibuat manual · channel: {channel}")
                order.status = "paid"
                order.save(update_fields=["status"])

            # auto-issue a tax invoice for this order
            try:
                from apps.tax.services import issue_invoice_for_order
                inv, _ = issue_invoice_for_order(order,
                    issued_on=order.created_at.date() if hasattr(order.created_at, 'date') else None,
                    notes=notes)
                # Patch buyer details not available in the order model
                patch = {}
                if buyer_npwp:
                    patch["buyer_npwp"] = buyer_npwp
                if company_name:
                    patch["buyer_name"] = f"{company_name} ({cust_name})" if cust_name else company_name
                if patch:
                    from apps.tax.models import Invoice as _Inv
                    _Inv.objects.filter(pk=inv.pk).update(**patch)
            except Exception:
                pass  # non-critical — order is saved regardless

            from django.contrib import messages as _msg
            _msg.success(request, f"Invoice {order.invoice_number} berhasil dibuat.")
            from django.shortcuts import redirect as _redir
            order.refresh_from_db()
            try:
                return _redir("merchant-invoice-detail", invoice_id=order.invoice.id)
            except Exception:
                return _redir("merchant-sales-invoices")

        except Exception as exc:
            from django.contrib import messages as _msg
            _msg.error(request, f"Gagal: {exc}")

    today = __import__("datetime").date.today().isoformat()
    return render(request, "merchant/invoice_new.html",
        _merchant_context(request, store, products=products,
                          channel_choices=CHANNEL_CHOICES, today=today))


@login_required(login_url="/merchant/login/")
def merchant_invoice_issue(request, order_id):
    """Issue (create) an invoice for an existing order that doesn't have one yet."""
    import uuid as _uuid
    from apps.orders.models import Order
    from apps.tax.services import issue_invoice_for_order

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)
    order = get_object_or_404(Order, id=_uuid.UUID(str(order_id)), store=store)

    if request.method == "POST":
        try:
            invoice, created = issue_invoice_for_order(order)
            if created:
                messages.success(request, f"Invoice {invoice.number} berhasil diterbitkan.")
            else:
                messages.info(request, f"Invoice {invoice.number} sudah ada.")
            return redirect("merchant-invoice-detail", invoice_id=invoice.id)
        except Exception as exc:
            messages.error(request, f"Gagal menerbitkan invoice: {exc}")
            return redirect("merchant-sales-invoices")

    raise Http404


@login_required(login_url="/merchant/login/")
def merchant_invoice_detail(request, invoice_id):
    """Sales invoice detail page — shows issued invoice with line items and payment history."""
    import uuid as _uuid
    from apps.tax.models import Invoice, InvoicePayment
    from apps.tax.services import record_payment, issue_invoice_for_order

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    # Invoice lookup by UUID
    try:
        invoice_uuid = _uuid.UUID(str(invoice_id))
        invoice = get_object_or_404(Invoice, id=invoice_uuid, store=store)
    except (ValueError, AttributeError):
        raise Http404

    if request.method == "POST":
        _require_roles(request, store, CHANNEL_ROLES)
        action = request.POST.get("action", "")

        if action == "pay":
            from decimal import Decimal, InvalidOperation
            import datetime
            try:
                amount = Decimal(request.POST.get("amount", "0").replace(",", ""))
                paid_on_str = request.POST.get("paid_on", "")
                paid_on = datetime.date.fromisoformat(paid_on_str) if paid_on_str else datetime.date.today()
                method = request.POST.get("method", "transfer")
                reference = request.POST.get("reference", "")[:120]
                note = request.POST.get("note", "")[:200]
                record_payment(invoice, amount, paid_on, method, reference, note, request.user)
                messages.success(request, "Pembayaran berhasil dicatat.")
            except Exception as exc:
                messages.error(request, f"Gagal: {exc}")
            return redirect("merchant-invoice-detail", invoice_id=invoice.id)

        if action == "void":
            invoice.status = Invoice.STATUS_VOID
            invoice.save(update_fields=["status"])
            messages.success(request, "Invoice dibatalkan.")
            return redirect("merchant-invoice-detail", invoice_id=invoice.id)

    items = invoice.items.select_related("product").all()
    payments = invoice.payments.all()
    amount_paid = sum(p.amount for p in payments)
    amount_due = invoice.total - amount_paid

    import datetime
    today = datetime.date.today().isoformat()

    return render(request, "merchant/invoice_detail.html", _merchant_context(request, store,
        invoice=invoice, items=items, payments=payments,
        amount_paid=amount_paid, amount_due=amount_due,
        today=today,
        payment_methods=InvoicePayment.METHOD_CHOICES,
    ))


@login_required(login_url="/merchant/login/")
def merchant_invoice_edit(request, invoice_id):
    """Edit an existing invoice — buyer details, dates, line items."""
    import uuid as _uuid
    from decimal import Decimal, InvalidOperation
    from apps.catalog.models import Product
    from apps.tax.models import Invoice, InvoiceItem

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)
    try:
        inv = get_object_or_404(Invoice, id=_uuid.UUID(str(invoice_id)), store=store)
    except (ValueError, AttributeError):
        raise Http404

    products = Product.objects.filter(store=store, is_active=True).order_by("name")

    if request.method == "POST":
        from django.db import transaction as _tx
        import datetime
        try:
            issued_str = request.POST.get("issued_on", "")
            due_str = request.POST.get("due_date", "")
            issued_on = datetime.date.fromisoformat(issued_str) if issued_str else inv.issued_on
            due_date = datetime.date.fromisoformat(due_str) if due_str else None

            buyer_name = request.POST.get("buyer_name", inv.buyer_name)[:160]
            buyer_address = request.POST.get("buyer_address", inv.buyer_address)[:300]
            buyer_npwp = request.POST.get("buyer_npwp", inv.buyer_npwp)[:20]
            notes = request.POST.get("notes", inv.notes)[:300]

            skus = request.POST.getlist("sku[]")
            qtys = request.POST.getlist("qty[]")
            prices = request.POST.getlist("unit_price[]")
            dims = request.POST.getlist("dimension[]")
            # Pad dims list to same length as skus
            dims += [""] * (len(skus) - len(dims))

            lines, subtotal = [], Decimal("0")
            for sku, qty_str, price_str, dim in zip(skus, qtys, prices, dims):
                sku = sku.strip()
                if not sku:
                    continue
                try:
                    qty = Decimal(qty_str)
                    unit_price = Decimal(price_str.replace(",", ""))
                except (ValueError, InvalidOperation):
                    continue
                if qty <= 0:
                    continue
                product = (Product.objects.filter(store=store, sku=sku).first()
                           or Product.objects.filter(store=store, name__iexact=sku).first())
                lt = unit_price * qty
                subtotal += lt
                lines.append({"product": product,
                               "sku": product.sku if product else sku,
                               "name": product.name if product else sku,
                               "qty": qty, "unit_price": unit_price, "line_total": lt,
                               "dimension": dim.strip()[:60]})

            if not lines:
                messages.error(request, "Tambahkan minimal satu produk.")
                return redirect("merchant-invoice-edit", invoice_id=inv.id)

            discount = Decimal("0")
            shipping = Decimal("0")
            try:
                discount = Decimal(request.POST.get("discount", "0").replace(",", ""))
                shipping = Decimal(request.POST.get("shipping", "0").replace(",", ""))
            except InvalidOperation:
                pass
            total = max(subtotal - discount, Decimal("0")) + shipping

            with _tx.atomic():
                inv.issued_on = issued_on
                inv.due_date = due_date
                inv.buyer_name = buyer_name
                inv.buyer_address = buyer_address
                inv.buyer_npwp = buyer_npwp
                inv.notes = notes
                inv.subtotal = subtotal
                inv.discount = discount
                inv.shipping = shipping
                inv.tax_base = subtotal - discount
                inv.total = total
                inv.save()

                inv.items.all().delete()
                for line in lines:
                    InvoiceItem.objects.create(
                        invoice=inv,
                        product=line["product"],
                        description=line["name"],
                        sku=line["sku"],
                        quantity=line["qty"],
                        unit_price=line["unit_price"],
                        line_total=line["line_total"],
                        dimension=line["dimension"],
                    )

                from apps.orders.models import Order as _Ord
                _Ord.objects.filter(pk=inv.order_id).update(
                    subtotal=subtotal, discount=discount,
                    shipping_cost=shipping, total=total)

            messages.success(request, "Invoice diperbarui.")
            return redirect("merchant-invoice-detail", invoice_id=inv.id)
        except Exception as exc:
            messages.error(request, f"Gagal: {exc}")

    return render(request, "merchant/invoice_edit.html", _merchant_context(request, store,
        invoice=inv, items=inv.items.all(), products=products,
        today=__import__("datetime").date.today().isoformat()))


@login_required(login_url="/merchant/login/")
def merchant_invoice_duplicate(request, invoice_id):
    """Duplicate an invoice — same items, optionally different customer."""
    import uuid as _uuid
    from apps.tax.models import Invoice

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)
    try:
        source = get_object_or_404(Invoice, id=_uuid.UUID(str(invoice_id)), store=store)
    except (ValueError, AttributeError):
        raise Http404

    if request.method == "POST":
        from django.db import transaction as _tx
        import datetime
        try:
            buyer_name = (request.POST.get("buyer_name", "") or source.buyer_name)[:160]
            buyer_address = request.POST.get("buyer_address", source.buyer_address)[:300]
            buyer_npwp = request.POST.get("buyer_npwp", source.buyer_npwp)[:20]
            customer_id = request.POST.get("customer_id", "")
            issued_str = request.POST.get("issued_on", "")
            issued_on = datetime.date.fromisoformat(issued_str) if issued_str else datetime.date.today()
            notes = request.POST.get("notes", source.notes)[:300]

            crm_customer = None
            if customer_id:
                from apps.shops.models import Customer as _Cust
                try:
                    crm_customer = _Cust.objects.get(id=customer_id, store=store)
                    buyer_name = crm_customer.name
                    buyer_address = crm_customer.address or buyer_address
                    buyer_npwp = crm_customer.npwp or buyer_npwp
                except Exception:
                    pass

            with _tx.atomic():
                from apps.orders.models import Order, OrderItem, OrderStatusEvent
                new_order = Order.objects.create(
                    store=store, channel=source.order.channel,
                    customer_name=buyer_name,
                    phone=source.order.phone,
                    shipping_address=buyer_address,
                    subtotal=source.subtotal, discount=source.discount,
                    shipping_cost=source.shipping, total=source.total,
                    payment_method="manual", delivery_notes=notes,
                    customer=crm_customer,
                    idempotency_key=_uuid.uuid4().hex,
                )
                for item in source.items.all():
                    OrderItem.objects.create(
                        order=new_order, product=item.product,
                        name=item.description, sku=item.sku,
                        unit_price=item.unit_price, quantity=int(item.quantity),
                        line_total=item.line_total,
                    )
                OrderStatusEvent.objects.create(order=new_order, status="paid",
                    note=f"Duplikat dari invoice {source.number}")
                new_order.status = "paid"
                new_order.save(update_fields=["status"])

                from apps.tax.services import issue_invoice_for_order
                new_inv, _ = issue_invoice_for_order(new_order, issued_on=issued_on, notes=notes)

                from apps.tax.models import Invoice as _Inv
                _Inv.objects.filter(pk=new_inv.pk).update(
                    buyer_name=buyer_name, buyer_address=buyer_address, buyer_npwp=buyer_npwp)

                # Copy dimensions from source items
                source_dims = {item.sku: item.dimension for item in source.items.all()}
                for ni in new_inv.items.all():
                    dim = source_dims.get(ni.sku, "")
                    if dim:
                        ni.dimension = dim
                        ni.save(update_fields=["dimension"])

            messages.success(request, f"Invoice berhasil diduplikat menjadi {new_inv.number}.")
            return redirect("merchant-invoice-detail", invoice_id=new_inv.id)
        except Exception as exc:
            messages.error(request, f"Gagal menduplikat invoice: {exc}")
            return redirect("merchant-invoice-detail", invoice_id=source.id)

    return render(request, "merchant/invoice_duplicate.html", _merchant_context(request, store,
        source=source, today=__import__("datetime").date.today().isoformat()))


@login_required(login_url="/merchant/login/")
def merchant_invoice_delete(request, invoice_id):
    """Void (or hard-delete drafts) an invoice."""
    import uuid as _uuid
    from apps.tax.models import Invoice

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)
    try:
        inv = get_object_or_404(Invoice, id=_uuid.UUID(str(invoice_id)), store=store)
    except (ValueError, AttributeError):
        raise Http404

    if request.method != "POST":
        raise Http404

    inv_num = inv.number
    if inv.status == Invoice.STATUS_DRAFT:
        inv.delete()
    else:
        inv.status = Invoice.STATUS_VOID
        inv.save(update_fields=["status"])
    messages.success(request, f"Invoice {inv_num} dihapus/dibatalkan.")
    return redirect("merchant-sales-invoices")


@login_required(login_url="/merchant/login/")
def merchant_invoice_batch(request):
    """Create multiple invoices at once — each with its own customer and items."""
    from decimal import Decimal, InvalidOperation
    from apps.catalog.models import Product
    from apps.orders.models import Order, OrderItem, OrderStatusEvent
    import uuid as _uuid_mod
    import json as _json

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES)
    products = Product.objects.filter(store=store, is_active=True).order_by("name")
    CHANNEL_CHOICES = [c for c in Order.CHANNELS if c[0] not in ("pos",)]

    if request.method == "POST":
        from django.db import transaction as _tx
        import datetime

        # The form sends invoices as JSON: invoices_json = [{customer_name, customer_id,
        # phone, address, npwp, company_name, invoice_date, channel, notes,
        # items:[{sku,qty,unit_price,dimension}], discount, shipping}]
        raw = request.POST.get("invoices_json", "[]")
        try:
            invoice_defs = _json.loads(raw)
        except Exception:
            messages.error(request, "Data invoice tidak valid.")
            return redirect("merchant-invoice-batch")

        if not invoice_defs:
            messages.error(request, "Tidak ada invoice untuk dibuat.")
            return redirect("merchant-invoice-batch")

        created_numbers = []
        errors = []

        for idx, inv_def in enumerate(invoice_defs, 1):
            try:
                channel      = inv_def.get("channel", "manual")
                inv_date     = inv_def.get("invoice_date", "")
                cust_name    = (inv_def.get("customer_name") or "Pelanggan")[:120]
                company_name = (inv_def.get("company_name") or "")[:160]
                buyer_npwp   = (inv_def.get("npwp") or "")[:20]
                phone        = (inv_def.get("phone") or "-")[:30] or "-"
                address      = (inv_def.get("address") or "-")[:300] or "-"
                notes        = (inv_def.get("notes") or "")[:300]
                ext_id       = (inv_def.get("external_order_id") or "")[:100]
                customer_id  = inv_def.get("customer_id", "")

                crm_customer = None
                if customer_id:
                    from apps.shops.models import Customer as _Cust
                    try:
                        crm_customer = _Cust.objects.get(id=customer_id, store=store)
                        upd = []
                        if address and address != "-" and not crm_customer.address:
                            crm_customer.address = address; upd.append("address")
                        if buyer_npwp and not crm_customer.npwp:
                            crm_customer.npwp = buyer_npwp; upd.append("npwp")
                        if company_name and not crm_customer.company_name:
                            crm_customer.company_name = company_name; upd.append("company_name")
                        if upd:
                            crm_customer.save(update_fields=upd)
                    except Exception:
                        crm_customer = None

                items_raw = inv_def.get("items", [])
                lines, subtotal = [], Decimal("0")
                for it in items_raw:
                    sku = (it.get("sku") or "").strip()
                    if not sku:
                        continue
                    try:
                        qty        = Decimal(str(it.get("qty", 1)))
                        unit_price = Decimal(str(it.get("unit_price", 0)).replace(",", ""))
                    except (ValueError, InvalidOperation):
                        continue
                    if qty <= 0:
                        continue
                    product = (Product.objects.filter(store=store, sku=sku).first()
                               or Product.objects.filter(store=store, name__iexact=sku).first())
                    lt = unit_price * qty
                    subtotal += lt
                    lines.append({"product": product,
                                  "sku": product.sku if product else sku,
                                  "name": product.name if product else sku,
                                  "qty": qty, "unit_price": unit_price, "line_total": lt,
                                  "dimension": (it.get("dimension") or "")[:60]})

                if not lines:
                    errors.append(f"Invoice #{idx}: tidak ada produk valid.")
                    continue

                try:
                    discount = Decimal(str(inv_def.get("discount", 0)).replace(",", ""))
                    shipping = Decimal(str(inv_def.get("shipping", 0)).replace(",", ""))
                except InvalidOperation:
                    discount = shipping = Decimal("0")
                total = max(subtotal - discount, Decimal("0")) + shipping

                with _tx.atomic():
                    order = Order.objects.create(
                        store=store, channel=channel,
                        customer_name=cust_name, phone=phone,
                        shipping_address=address,
                        subtotal=subtotal, discount=discount,
                        shipping_cost=shipping, total=total,
                        payment_method="manual", delivery_notes=notes,
                        external_order_id=ext_id,
                        customer=crm_customer,
                        idempotency_key=_uuid_mod.uuid4().hex,
                    )
                    if inv_date:
                        from django.utils.dateparse import parse_date as _pd
                        d = _pd(inv_date)
                        if d:
                            Order.objects.filter(pk=order.pk).update(created_at=d)
                            order.refresh_from_db()
                    order.save()
                    for line in lines:
                        OrderItem.objects.create(
                            order=order, product=line["product"],
                            name=line["name"], sku=line["sku"],
                            unit_price=line["unit_price"], quantity=int(line["qty"]),
                            line_total=line["line_total"],
                        )
                    OrderStatusEvent.objects.create(order=order, status="paid",
                        note=f"Invoice batch · channel: {channel}")
                    order.status = "paid"
                    order.save(update_fields=["status"])

                    try:
                        from apps.tax.services import issue_invoice_for_order
                        inv, _ = issue_invoice_for_order(order,
                            issued_on=order.created_at.date() if hasattr(order.created_at, "date") else None,
                            notes=notes)
                        patch = {}
                        if buyer_npwp:
                            patch["buyer_npwp"] = buyer_npwp
                        if company_name:
                            patch["buyer_name"] = f"{company_name} ({cust_name})" if cust_name else company_name
                        if patch:
                            from apps.tax.models import Invoice as _Inv
                            _Inv.objects.filter(pk=inv.pk).update(**patch)
                        created_numbers.append(inv.number)
                    except Exception:
                        created_numbers.append(order.invoice_number or str(order.pk)[:8])

            except Exception as exc:
                errors.append(f"Invoice #{idx}: {exc}")

        if errors:
            for e in errors:
                messages.error(request, e)
        if created_numbers:
            messages.success(request, f"{len(created_numbers)} invoice berhasil dibuat: {', '.join(created_numbers)}.")
            return redirect("merchant-sales-invoices")
        return redirect("merchant-invoice-batch")

    today = __import__("datetime").date.today().isoformat()
    return render(request, "merchant/invoice_batch.html",
        _merchant_context(request, store, products=products,
                          channel_choices=CHANNEL_CHOICES, today=today))


@login_required(login_url="/merchant/login/")
def merchant_invoices_csv(request):
    """CSV export of the sales invoice list."""
    import csv as _csv
    from django.utils.timezone import localtime
    from apps.orders.models import Order

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    qs = Order.objects.filter(store=store).select_related("invoice", "customer").order_by("-created_at")
    channel_filter = request.GET.get("channel", "")
    if channel_filter:
        qs = qs.filter(channel=channel_filter)
    month_filter = request.GET.get("month", "")
    if month_filter:
        from django.utils.dateparse import parse_date as _pd
        d = _pd(month_filter + "-01")
        if d:
            qs = qs.filter(created_at__year=d.year, created_at__month=d.month)
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(customer_name__icontains=q) | Q(invoice_number__icontains=q) |
            Q(external_order_id__icontains=q)
        )

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="invoice-penjualan.csv"'
    response.write("﻿")  # BOM for Excel
    writer = _csv.writer(response)
    writer.writerow(["Tanggal", "No. Pesanan", "Channel", "Pembeli", "No. HP",
                     "Subtotal", "Diskon", "Ongkir", "Total", "Status Pesanan",
                     "No. Invoice", "Status Invoice"])
    channel_map = dict(Order.CHANNELS)
    for order in qs:
        inv = None
        try:
            inv = order.invoice
        except Exception:
            pass
        writer.writerow([
            localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
            order.invoice_number or str(order.id)[:8],
            channel_map.get(order.channel, order.channel),
            order.customer_name,
            order.phone or "",
            order.subtotal, order.discount, order.shipping_cost, order.total,
            order.get_status_display(),
            inv.number if inv else "",
            inv.get_status_display() if inv else "",
        ])
    return response


@login_required(login_url="/merchant/login/")
def merchant_sales_report(request):
    """Monthly sales report — revenue by channel and invoice status per month."""
    from decimal import Decimal
    from collections import defaultdict
    import calendar
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncMonth
    from apps.orders.models import Order
    from apps.tax.models import Invoice

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    year_filter = request.GET.get("year", "")
    try:
        year = int(year_filter)
    except (ValueError, TypeError):
        from django.utils import timezone
        year = timezone.localdate().year

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed",
                        "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    base_qs = Order.objects.filter(store=store, status__in=REVENUE_STATUSES,
                                   created_at__year=year)

    monthly_rows = list(base_qs
        .annotate(month=TruncMonth("created_at"))
        .values("month", "channel")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("month", "channel"))

    months = list(range(1, 13))
    channels_used = sorted({r["channel"] for r in monthly_rows})
    channel_label = dict(Order.CHANNELS)

    # matrix[month_int][channel] = revenue
    matrix = defaultdict(lambda: defaultdict(Decimal))
    month_totals = defaultdict(Decimal)
    for row in monthly_rows:
        m = row["month"].month
        matrix[m][row["channel"]] = row["revenue"] or Decimal(0)
        month_totals[m] += row["revenue"] or Decimal(0)

    # Invoice status per month
    inv_rows = list(Invoice.objects.filter(store=store, issued_on__year=year)
        .values("issued_on__month", "status")
        .annotate(count=Count("id"), total=Sum("total"))
        .order_by("issued_on__month"))
    inv_by_month = defaultdict(dict)
    for r in inv_rows:
        inv_by_month[r["issued_on__month"]][r["status"]] = r["count"]

    year_total = sum(month_totals.values(), Decimal(0))

    # Pre-compute table rows — no custom template filters needed
    table_rows = []
    for m in months:
        channel_vals = [(ch, channel_label.get(ch, ch), matrix[m].get(ch, Decimal(0)))
                        for ch in channels_used]
        inv_s = inv_by_month.get(m, {})
        inv_total = sum(inv_s.values())
        table_rows.append({
            "month": m,
            "name": calendar.month_name[m],
            "abbr": calendar.month_abbr[m],
            "channels": channel_vals,
            "total": month_totals.get(m, Decimal(0)),
            "inv_paid": inv_s.get("paid", 0),
            "inv_issued": inv_s.get("issued", 0),
            "inv_partial": inv_s.get("partial", 0),
            "inv_overdue": inv_s.get("overdue", 0),
            "inv_total": inv_total,
            "has_data": bool(month_totals.get(m) or inv_total),
        })

    first_order = Order.objects.filter(store=store).order_by("created_at").first()
    first_year = first_order.created_at.year if first_order else year
    available_years = list(range(first_year, year + 1))

    return render(request, "merchant/sales_report.html", _merchant_context(request, store,
        year=year, available_years=available_years,
        channels_used=channels_used, channel_label=channel_label,
        table_rows=table_rows, year_total=year_total,
    ))


@login_required(login_url="/merchant/login/")
def merchant_channel_stock(request):
    """Stock tracker — units sold per product broken down by channel."""
    from decimal import Decimal
    from django.db.models import Q, Sum, Count
    from django.utils.dateparse import parse_date as _pd
    from apps.orders.models import Order, OrderItem
    from apps.catalog.models import Product

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed",
                        "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    # --- Date range filter ---
    date_from_raw = request.GET.get("date_from", "")
    date_to_raw = request.GET.get("date_to", "")
    from datetime import date, timedelta
    from django.utils import timezone
    today = timezone.localdate()
    try:
        date_from = _pd(date_from_raw) if date_from_raw else today.replace(day=1)
    except (ValueError, AttributeError):
        date_from = today.replace(day=1)
    try:
        date_to = _pd(date_to_raw) if date_to_raw else today
    except (ValueError, AttributeError):
        date_to = today

    # Channels present in orders for this store
    channel_label = dict(Order.CHANNELS)
    used_channels = list(
        Order.objects.filter(store=store, status__in=REVENUE_STATUSES)
        .values_list("channel", flat=True).distinct().order_by("channel")
    )

    # Units sold per product per channel in the period
    sales_qs = (
        OrderItem.objects.filter(
            order__store=store,
            order__status__in=REVENUE_STATUSES,
            order__created_at__date__gte=date_from,
            order__created_at__date__lte=date_to,
        )
        .values("sku", "name", "order__channel")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"))
        .order_by("sku", "order__channel")
    )

    # Pivot into {sku: {channel: {units, revenue}}}
    from collections import defaultdict
    pivot = defaultdict(lambda: {"name": "", "total_units": 0, "total_revenue": Decimal(0),
                                  "channels": defaultdict(lambda: {"units": 0, "revenue": Decimal(0)})})
    for row in sales_qs:
        sku = row["sku"] or row["name"]
        pivot[sku]["name"] = row["name"]
        pivot[sku]["channels"][row["order__channel"]]["units"] += row["units"] or 0
        pivot[sku]["channels"][row["order__channel"]]["revenue"] += row["revenue"] or Decimal(0)
        pivot[sku]["total_units"] += row["units"] or 0
        pivot[sku]["total_revenue"] += row["revenue"] or Decimal(0)

    # Attach current available stock from catalog
    products_by_sku = {
        p.sku: p for p in Product.objects.filter(store=store, is_active=True)
    }

    # Build rows sorted by total units desc
    rows = []
    for sku, data in sorted(pivot.items(), key=lambda x: -x[1]["total_units"]):
        product = products_by_sku.get(sku)
        channel_vals = [(ch, data["channels"][ch]["units"], data["channels"][ch]["revenue"])
                        for ch in used_channels]
        rows.append({
            "sku": sku,
            "name": data["name"],
            "total_units": data["total_units"],
            "total_revenue": data["total_revenue"],
            "available_stock": product.available_stock if product else None,
            "channels": channel_vals,
        })

    # Aggregate channel totals directly from DB
    ch_summary = (
        OrderItem.objects.filter(
            order__store=store,
            order__status__in=REVENUE_STATUSES,
            order__created_at__date__gte=date_from,
            order__created_at__date__lte=date_to,
        )
        .values("order__channel")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"), orders=Count("order", distinct=True))
        .order_by("-revenue")
    )
    ch_summary_rows = [
        {"channel": r["order__channel"],
         "label": channel_label.get(r["order__channel"], r["order__channel"]),
         "units": r["units"] or 0,
         "revenue": r["revenue"] or Decimal(0),
         "orders": r["orders"] or 0}
        for r in ch_summary
    ]
    total_units = sum(r["units"] for r in ch_summary_rows)

    channel_headers = [(ch, channel_label.get(ch, ch)) for ch in used_channels]

    return render(request, "merchant/channel_stock.html", _merchant_context(request, store,
        rows=rows, used_channels=used_channels, channel_label=channel_label,
        channel_headers=channel_headers,
        ch_summary_rows=ch_summary_rows, total_units=total_units,
        date_from=date_from, date_to=date_to,
    ))
