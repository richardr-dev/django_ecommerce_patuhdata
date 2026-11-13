import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.billing.plans import channel_limit_error
from apps.catalog.views import paginate
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .adapters import PLATFORMS
from .forms import OrdersImportForm, SalesChannelForm
from .models import ChannelBuyer, ImportBatch, SalesChannel
from .services import (
    CSV_COLUMNS,
    ChannelImportError,
    get_or_create_channel,
    import_orders,
    match_listings,
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
                    from .parsers import parse_order_file
                    orders, skipped = parse_order_file(channel.platform, upload)
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
            "label": "TikTok Shop", "icon": "⬛", "file_type": "xlsx",
            "color": "#010101", "text_color": "#fff",
            "tagline": "Ekspor langsung dari Seller Center",
            "badge": ".xlsx",
            "instructions": [
                "Buka TikTok Shop Seller Center → Pesanan → Semua pesanan",
                "Klik Ekspor, pilih rentang tanggal",
                "Unduh file .xlsx yang dihasilkan",
                "Upload langsung di sini — tidak perlu reformatting",
            ],
            "accept": ".xlsx,.csv",
        },
        "lazada": {
            "label": "Lazada", "icon": "🔵", "file_type": "xlsx",
            "color": "#0F146D", "text_color": "#fff",
            "tagline": "Ekspor langsung dari Seller Center",
            "badge": ".xlsx / .csv",
            "instructions": [
                "Buka Lazada Seller Center → Pesanan → Semua pesanan",
                "Klik Ekspor, pilih rentang tanggal",
                "Unduh file hasil ekspor (.xlsx atau .csv)",
                "Upload langsung di sini — tidak perlu reformatting",
            ],
            "accept": ".xlsx,.csv",
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
                from .parsers import parse_order_file
                orders, skipped = parse_order_file(platform, upload)

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
                             "duplicates": summary["duplicates"],
                             "updated": summary["updated"], "cancelled": summary["cancelled"],
                             "skipped": skipped,
                             "soft": soft, "errors": len(summary["errors"])})
                if summary["created"] or summary["updated"]:
                    messages.success(request,
                        f"{summary['created']} pesanan baru dari {selected['label']} diimpor, "
                        f"{summary['updated']} pesanan diperbarui statusnya"
                        + (f", {summary['cancelled']} dibatalkan/diretur dan stoknya dikembalikan."
                           if summary["cancelled"] else "."))
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

    from datetime import date, timedelta
    from decimal import Decimal

    from django.db.models import Count, Sum
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
    revenue = totals["revenue"] or Decimal(0)
    order_count = totals["orders"] or 0
    aov = revenue / order_count if order_count else Decimal(0)

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
    peak = max((r["revenue"] for r in daily), default=Decimal(0))
    for row in daily:
        row["pct"] = round(float(row["revenue"] / peak * 100), 1) if peak else 0

    # --- Monthly trend ---
    monthly = list(base_qs
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("-month")[:12])
    monthly_peak = max((r["revenue"] for r in monthly), default=Decimal(0))
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
        total_discount=totals["discount"] or Decimal(0),
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

    from datetime import date, timedelta
    from decimal import Decimal

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
    revenue = totals["revenue"] or Decimal(0)
    order_count = totals["orders"] or 0
    aov = revenue / order_count if order_count else Decimal(0)

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
        "total_discount": totals["discount"] or Decimal(0),
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
            from apps.orders.models import Order as _Ord
            from apps.tax.services import issue_invoice_for_order
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

    from django.db.models import Count as _Count
    from django.db.models import Sum as _Sum
    invoice_stats = list(_Inv.objects.filter(store=store).values("status").annotate(
        count=_Count("id"), total=_Sum("total")).order_by("status"))

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed",
                        "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    # --- Filters: one shared implementation (invoice_filters) for the page and its exports ---
    from django.db.models import Q, Value
    from django.db.models.functions import Coalesce

    from apps.tax.models import InvoicePayment as _Pay

    from apps.accounting.views import render_partial

    from .invoice_filters import KEYS, PRESETS, SORTS, TABS, apply_filters, apply_sort, apply_tab, values

    params = request.GET
    qs = Order.objects.filter(store=store).select_related("invoice", "customer").order_by("-created_at")
    qs, active_filters = apply_filters(qs, params, _dt.date.today())
    channel_filter, status_filter = values(params, "channel"), params.get("status", "")
    inv_filter = params.get("inv", "")
    tab_base = qs
    invoice_tabs = [(code, label, tab_base.filter(cond).count() if cond is not None
        else tab_base.count()) for code, label, cond in TABS]
    qs = apply_sort(apply_tab(qs, inv_filter), params.get("sort", ""))
    if inv_filter and inv_filter in dict((code, label) for code, label, _ in TABS):
        active_filters.append({"label": dict((code, label) for code, label, _ in TABS)[inv_filter],
            "keys": ["inv"]})

    # Money still owed on the invoices in view, and how much of it is late.
    open_statuses = [_Inv.STATUS_ISSUED, _Inv.STATUS_PARTIAL, _Inv.STATUS_OVERDUE]
    open_invoices = _Inv.objects.filter(store=store, order__in=qs, status__in=open_statuses)
    def _owed(invoices):
        billed = invoices.aggregate(t=Sum("total"))["t"] or Decimal(0)
        paid = _Pay.objects.filter(invoice__in=invoices).aggregate(t=Sum("amount"))["t"] or Decimal(0)
        from apps.tax.models import CreditNote as _CN
        credited = _CN.objects.filter(invoice__in=invoices).exclude(status=_CN.STATUS_VOID
            ).aggregate(t=Sum("applied"))["t"] or Decimal(0)
        return billed - paid - credited
    outstanding = _owed(open_invoices)
    overdue_total = _owed(open_invoices.filter(status=_Inv.STATUS_OVERDUE))
    paid_total = _Inv.objects.filter(store=store, order__in=qs, status=_Inv.STATUS_PAID
        ).aggregate(t=Sum("total"))["t"] or Decimal(0)

    rev_qs = qs.filter(status__in=REVENUE_STATUSES)
    totals = rev_qs.aggregate(revenue=Sum("total"), orders=Count("id"))
    revenue = totals["revenue"] or Decimal(0)
    order_count = totals["orders"] or 0

    channel_label_map = dict(Order.CHANNELS)
    used_channels = (Order.objects.filter(store=store)
                     .values_list("channel", flat=True).distinct().order_by("channel"))
    channel_choices = [(c, channel_label_map.get(c, c)) for c in used_channels]
    used_statuses = (Order.objects.filter(store=store)
                     .values_list("status", flat=True).distinct().order_by("status"))
    status_label_map = dict(Order._meta.get_field("status").choices)
    status_choices = [(s, status_label_map.get(s, s)) for s in used_statuses]

    recent_batches = (ImportBatch.objects.filter(store=store)
                      .select_related("channel").order_by("-imported_at")[:5])

    qs = qs.annotate(inv_paid=Coalesce(Sum("invoice__payments__amount"), Value(Decimal(0))))
    page, page_range, page_query = paginate(request, qs, 50)
    today = _dt.date.today()
    for order in page.object_list:
        invoice = getattr(order, "invoice", None)
        order.inv_due = invoice.amount_due if invoice and invoice.status not in (
            _Inv.STATUS_VOID, _Inv.STATUS_DRAFT) else None
        order.overdue_days = ((today - invoice.due_date).days if invoice and invoice.due_date
            and invoice.due_date < today and order.inv_due else 0)

    from urllib.parse import urlencode
    filter_query = urlencode([(key, value) for key in KEYS
        for value in (values(params, key) if key == "channel" else [params.get(key, "")]) if value])

    chip_rows = (_Inv.objects.filter(store=store).exclude(status__in=(_Inv.STATUS_DRAFT,
        _Inv.STATUS_VOID)).values("order__channel").annotate(n=Count("id")).order_by("-n"))
    channel_chips = [{"code": r["order__channel"], "label": channel_label_map.get(
        r["order__channel"], r["order__channel"]), "count": r["n"],
        "active": r["order__channel"] in channel_filter} for r in chip_rows]
    channel_total = sum(chip["count"] for chip in channel_chips)

    from .invoice_filters import date_range
    start, end, _label = date_range(params, today)
    return render_partial(request, "merchant/sales_invoices.html", "merchant/_sales_invoices_list.html",
        _merchant_context(request, store,
        orders=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        channel_filter=channel_filter, status_filter=status_filter, inv_filter=inv_filter,
        presets=PRESETS, sorts=SORTS, sort=params.get("sort", "newest"), crm=params.get("crm", ""),
        active_filters=active_filters, range_preset=params.get("range", ""),
        oob=bool(request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request")),
        date_from=params.get("date_from") or (start.isoformat() if start and not params.get("range") else ""),
        date_to=params.get("date_to") or (end.isoformat() if end and not params.get("range") else ""),
        min_total=params.get("min_total", ""), max_total=params.get("max_total", ""),
        query=params.get("q", "").strip(), filter_query=filter_query,
        channel_chips=channel_chips, channel_total=channel_total,
        channel_choices=channel_choices, status_choices=status_choices,
        revenue=revenue, order_count=order_count, outstanding=outstanding,
        overdue_total=overdue_total, paid_total=paid_total,
        invoice_tabs=invoice_tabs, recent_batches=recent_batches,
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
    from apps.tax.services import record_payment

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
            import datetime
            from decimal import Decimal
            try:
                amount = Decimal(request.POST.get("amount", "0").replace(",", ""))
                paid_on_str = request.POST.get("paid_on", "")
                paid_on = datetime.date.fromisoformat(paid_on_str) if paid_on_str else datetime.date.today()
                method = request.POST.get("method", "transfer")
                reference = request.POST.get("reference", "")[:120]
                note = request.POST.get("note", "")[:200]
                from apps.accounting.bank import bank_accounts
                bank = next((b for b in bank_accounts(store)
                    if str(b.pk) == request.POST.get("bank_account")), None)
                record_payment(invoice, amount, paid_on, method, reference, note, request.user,
                    bank_account=bank)
                messages.success(request, "Pembayaran berhasil dicatat.")
            except Exception as exc:
                messages.error(request, f"Gagal: {exc}")
            return redirect("merchant-invoice-detail", invoice_id=invoice.id)

        if action == "void":
            from apps.accounting.ledger import LedgerError
            from apps.tax.posting import void_invoice
            try:
                void_invoice(invoice, actor=request.user)
            except LedgerError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "Invoice dibatalkan.")
            return redirect("merchant-invoice-detail", invoice_id=invoice.id)

    items = invoice.items.select_related("product").all()
    payments = invoice.payments.all()
    amount_paid = sum(p.amount for p in payments)
    credited = invoice.credited
    amount_due = invoice.amount_due if invoice.status != "void" else invoice.total - amount_paid

    import datetime
    today = datetime.date.today().isoformat()

    from apps.accounting.bank import bank_accounts
    from apps.accounting.ledger import entries_for
    from apps.tax.receipts import credit_balance
    order = invoice.order
    credit_notes = list(invoice.credit_notes.all())
    journal = entries_for(store, [("invoice", invoice.pk)]
        + [("credit_note", note.pk) for note in credit_notes]
        + [("invoice_payment", payment.pk) for payment in payments]
        + [("payment", tx.pk) for tx in order.payment_transactions.all()]
        + [("refund", refund.pk) for refund in order.refunds.all()])
    paid_pct = min(100, int((amount_paid + credited) * 100 / invoice.total)) if invoice.total else 0
    from .invoice_recap import marketplace_summary
    return render(request, "merchant/invoice_detail.html", _merchant_context(request, store,
        marketplace=marketplace_summary(store, order),
        invoice=invoice, items=items, payments=payments, order=order, credited=credited,
        credit_notes=credit_notes, banks=bank_accounts(store),
        credit_available=max(credit_balance(store, order.customer, invoice.buyer_name), 0),
        amount_paid=amount_paid, amount_due=amount_due, paid_pct=paid_pct,
        today=today, journal=journal,
        payment_methods=InvoicePayment.METHOD_CHOICES,
    ))


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
        from apps.accounting.ledger import LedgerError
        from apps.tax.posting import void_invoice
        try:
            void_invoice(inv, actor=request.user)
        except LedgerError as error:
            messages.error(request, str(error))
            return redirect("merchant-invoice-detail", invoice_id=inv.id)
    messages.success(request, f"Invoice {inv_num} dihapus/dibatalkan.")
    return redirect("merchant-sales-invoices")


@login_required(login_url="/merchant/login/")
def merchant_invoices_csv(request):
    """CSV export of the sales invoice list."""
    import csv as _csv

    from django.utils.timezone import localtime

    from apps.orders.models import Order

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    from .invoice_filters import apply_filters, apply_tab
    qs = Order.objects.filter(store=store).select_related("invoice", "customer").order_by("-created_at")
    qs, _active = apply_filters(qs, request.GET)
    qs = apply_tab(qs, request.GET.get("inv", ""))

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


MONTHS_ID = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
    "September", "Oktober", "November", "Desember")


@login_required(login_url="/merchant/login/")
def merchant_sales_report(request):
    """Monthly sales report — revenue by channel and invoice status per month."""
    from collections import defaultdict
    from decimal import Decimal

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
    channel_headers = [(ch, channel_label.get(ch, ch)) for ch in channels_used]

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
            "name": MONTHS_ID[m - 1],
            "abbr": MONTHS_ID[m - 1][:3],
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
        channels_used=channels_used, channel_label=channel_label, channel_headers=channel_headers,
        table_rows=table_rows, year_total=year_total,
    ))


@login_required(login_url="/merchant/login/")
def merchant_channel_stock(request):
    """Stock tracker — units sold per product broken down by channel."""
    from decimal import Decimal

    from django.db.models import Count, Sum
    from django.utils.dateparse import parse_date as _pd

    from apps.catalog.models import Product
    from apps.orders.models import Order, OrderItem

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed",
                        "shipped", "delivered", "completed",
                        "refund_requested", "partially_refunded", "refunded"}

    # --- Date range filter ---
    date_from_raw = request.GET.get("date_from", "")
    date_to_raw = request.GET.get("date_to", "")

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


# ── Invoice PDF export ────────────────────────────────────────────────────────

def _resolve_invoice_qs(request, store):
    """Return Invoice queryset from ?ids=... or current filter params (channel/month/status/date/q)."""
    from apps.tax.models import Invoice
    ids_param = request.GET.get("ids", "").strip()
    qs = Invoice.objects.filter(store=store).select_related(
        "store", "order"
    ).prefetch_related("items")
    if ids_param:
        import uuid as _uuid
        ids = []
        for raw in ids_param.split(","):
            raw = raw.strip()
            if raw:
                try:
                    ids.append(_uuid.UUID(raw))
                except ValueError:
                    pass
        qs = qs.filter(id__in=ids)
    else:
        # The same filter as the list page, so what you see is what you export.
        from apps.orders.models import Order

        from .invoice_filters import apply_filters, apply_tab
        orders, _active = apply_filters(Order.objects.filter(store=store), request.GET)
        orders = apply_tab(orders, request.GET.get("inv", ""))
        qs = qs.filter(order__in=orders)
    return qs.order_by("-issued_on", "-number")


@login_required(login_url="/merchant/login/")
def merchant_invoice_pdf(request, invoice_id):
    """Single-invoice PDF download."""
    import uuid as _uuid

    from django.template.loader import render_to_string
    from weasyprint import HTML

    from apps.tax.models import Invoice

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})
    invoice = get_object_or_404(Invoice, id=_uuid.UUID(str(invoice_id)), store=store)
    invoice.items_list = list(invoice.items.all())

    html_str = render_to_string("merchant/invoice_pdf.html", {"invoices": [invoice]}, request=request)
    pdf_bytes = HTML(string=html_str, base_url=request.build_absolute_uri("/")).write_pdf()

    fname = f"invoice-{invoice.number}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@login_required(login_url="/merchant/login/")
def merchant_invoices_pdf(request):
    """Bulk PDF export — all selected invoices merged into one file, one invoice per page."""
    import io

    from django.template.loader import render_to_string
    from pypdf import PdfReader, PdfWriter
    from weasyprint import HTML

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    invoices = list(_resolve_invoice_qs(request, store))
    if not invoices:
        from django.contrib import messages as _msg
        _msg.warning(request, "Tidak ada invoice yang dipilih.")
        return redirect("merchant-sales-invoices")

    # Render each invoice separately so page numbers reset cleanly, then merge
    writer = PdfWriter()
    for inv in invoices:
        html_str = render_to_string("merchant/invoice_pdf.html", {"invoices": [inv]}, request=request)
        pdf_bytes = HTML(string=html_str, base_url=request.build_absolute_uri("/")).write_pdf()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    fname = f"invoices-{store.slug}-{today}-{len(invoices)}pcs.pdf"
    response = HttpResponse(buf.read(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


# ── Invoice XLSX export ───────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def merchant_invoices_xlsx(request):
    """Bulk XLSX export — summary sheet + line-items sheet."""
    import datetime
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    store = _staff_store(request)
    _require_roles(request, store, CHANNEL_ROLES | {"viewer"})

    invoices = list(_resolve_invoice_qs(request, store))
    if not invoices:
        from django.contrib import messages as _msg
        _msg.warning(request, "Tidak ada invoice yang dipilih.")
        return redirect("merchant-sales-invoices")

    wb = Workbook()

    # ── Styles ──────────────────────────────────────────────────────────────
    hdr_font   = Font(bold=True, color="FFFFFF", size=10)
    hdr_fill   = PatternFill("solid", fgColor="111827")
    sub_fill   = PatternFill("solid", fgColor="F3F4F6")
    bold_font  = Font(bold=True, size=10)
    money_fmt  = '#,##0'
    date_fmt   = 'DD-MMM-YYYY'
    thin_side  = Side(border_style="thin", color="E5E7EB")
    thin_border = Border(bottom=thin_side)

    def _header_row(ws, cols):
        ws.append([c[0] for c in cols])
        row = ws.max_row
        for ci, (_, width) in enumerate(cols, 1):
            cell = ws.cell(row, ci)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(ci)].width = width
        ws.row_dimensions[row].height = 20

    def _style_data_rows(ws, start, end, money_cols=()):
        for r in range(start, end + 1):
            for c in range(1, ws.max_column + 1):
                cell = ws.cell(r, c)
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")
                if c in money_cols:
                    cell.number_format = money_fmt
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            ws.row_dimensions[r].height = 16

    # ── Sheet 1: Summary ─────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Ringkasan Invoice"
    ws1.freeze_panes = "A2"

    summary_cols = [
        ("Tanggal", 14), ("No. Invoice", 22), ("Status", 14),
        ("Pembeli", 28), ("No. HP", 16), ("Channel", 14),
        ("No. Order", 22), ("Ref. Eksternal", 22),
        ("Subtotal (Rp)", 16), ("Diskon (Rp)", 14), ("Ongkir (Rp)", 14),
        ("PPN (Rp)", 12), ("Total (Rp)", 16),
        ("Sudah Dibayar (Rp)", 18), ("Sisa Tagihan (Rp)", 18),
    ]
    _header_row(ws1, summary_cols)

    for inv in invoices:
        paid = inv.amount_paid
        ws1.append([
            inv.issued_on,
            inv.number,
            inv.get_status_display(),
            inv.buyer_name,
            inv.order.phone or "",
            inv.order.get_channel_display(),
            inv.order.invoice_number or "",
            inv.order.external_order_id or "",
            float(inv.subtotal),
            float(inv.discount),
            float(inv.shipping),
            float(inv.tax),
            float(inv.total),
            float(paid),
            float(inv.amount_due),
        ])
        r = ws1.max_row
        ws1.cell(r, 1).number_format = date_fmt
        ws1.row_dimensions[r].height = 16

    money_cols_s1 = (9, 10, 11, 12, 13, 14, 15)
    _style_data_rows(ws1, 2, ws1.max_row, money_cols_s1)

    # Totals row
    n = ws1.max_row
    ws1.append([
        None, None, None, f"TOTAL ({len(invoices)} invoice)", None, None, None, None,
        f"=SUM(I2:I{n})", f"=SUM(J2:J{n})", f"=SUM(K2:K{n})",
        f"=SUM(L2:L{n})", f"=SUM(M2:M{n})", f"=SUM(N2:N{n})", f"=SUM(O2:O{n})",
    ])
    total_row = ws1.max_row
    for ci in range(1, 16):
        cell = ws1.cell(total_row, ci)
        cell.font = Font(bold=True, size=10)
        cell.fill = sub_fill
        if ci in money_cols_s1:
            cell.number_format = money_fmt
            cell.alignment = Alignment(horizontal="right", vertical="center")
    ws1.row_dimensions[total_row].height = 18

    # ── Sheet 2: Line Items ──────────────────────────────────────────────────
    ws2 = wb.create_sheet("Rincian Item")
    ws2.freeze_panes = "A2"

    item_cols = [
        ("Tanggal", 14), ("No. Invoice", 22), ("Pembeli", 26),
        ("SKU", 16), ("Deskripsi", 36), ("Dimensi", 20),
        ("Qty", 8), ("Harga Satuan (Rp)", 18), ("Diskon Item (Rp)", 16), ("Subtotal Item (Rp)", 18),
    ]
    _header_row(ws2, item_cols)

    for inv in invoices:
        for item in inv.items.all():
            ws2.append([
                inv.issued_on,
                inv.number,
                inv.buyer_name,
                item.sku or "",
                item.description,
                item.dimension or "",
                float(item.quantity),
                float(item.unit_price),
                float(item.discount),
                float(item.line_total),
            ])
            r = ws2.max_row
            ws2.cell(r, 1).number_format = date_fmt
            ws2.row_dimensions[r].height = 16

    money_cols_s2 = (8, 9, 10)
    _style_data_rows(ws2, 2, ws2.max_row, money_cols_s2)
    ws2.cell(ws2.max_row + 0, 7)  # ensure column G width set

    # ── Output ───────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    today_str = datetime.date.today().strftime("%Y%m%d")
    fname = f"invoices-{store.slug}-{today_str}.xlsx"
    response = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response
