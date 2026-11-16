"""Back office: customer segments, tags and email broadcasts; storefront: unsubscribe."""
import datetime
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.shops.permissions import roles

from .models import AuditLog, Customer, EmailBroadcast
from .segments import (
    add_tag,
    broadcast_audience,
    customers_in,
    describe,
    parse,
    queue_broadcast,
    read_unsubscribe,
)

VIEW_ROLES = roles("customers.view")
EDIT_ROLES = roles("customers.edit")
BROADCAST_ROLES = roles("customers.broadcast")


@login_required(login_url="/login/")
def merchant_customers(request):
    from apps.catalog.views import paginate
    from apps.orders.views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)

    # Approval queue actions
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action in ("approve", "approve_all", "block") and request.POST.get("customer_id") or action == "approve_all":
            _require_roles(request, store, EDIT_ROLES)
            if action == "approve_all":
                updated = Customer.objects.filter(store=store, status=Customer.STATUS_PENDING).update(
                    status=Customer.STATUS_ACTIVE)
                messages.success(request, f"{updated} pelanggan disetujui.")
            else:
                customer = get_object_or_404(Customer, id=request.POST["customer_id"], store=store)
                customer.status = Customer.STATUS_ACTIVE if action == "approve" else Customer.STATUS_BLOCKED
                customer.save(update_fields=["status"])
                label = "disetujui" if action == "approve" else "diblokir"
                messages.success(request, f"Pelanggan {customer.name} {label}.")
            return redirect("merchant-customers")

        if action == "sync_customers":
            _require_roles(request, store, EDIT_ROLES)
            from apps.shops.crm import backfill_customers
            count = backfill_customers(store)
            messages.success(request, f"{count} pesanan berhasil dihubungkan ke profil pelanggan.")
            return redirect("merchant-customers")

        if request.POST.get("add_tag"):
            _require_roles(request, store, EDIT_ROLES)
            segment = parse(request.POST)
            changed = add_tag(customers_in(store, segment).order_by("-created_at"), request.POST["add_tag"])
            messages.success(request, f"Tag ditambahkan ke {changed} pelanggan.")
            return redirect(f"{request.path}?{urlencode(segment)}")

    segment = parse(request.GET)
    # Active / blocked customers only in the main list
    # Marketplace buyers wait in the approval queue above; looking for people by where they buy or
    # live must still find them, so those filters (or the switch) include the waiting ones.
    pending_param = request.GET.get("pending")
    include_pending = pending_param == "1" or (pending_param is None and bool(
        segment.get("channel") or segment.get("location")))
    statuses = [Customer.STATUS_ACTIVE, Customer.STATUS_BLOCKED]
    if include_pending:
        statuses.append(Customer.STATUS_PENDING)
    customers = customers_in(store, segment).filter(status__in=statuses).order_by("-created_at")
    pending = Customer.objects.filter(store=store, status=Customer.STATUS_PENDING).select_related(
        "channel_buyer").order_by("-created_at")

    from apps.accounting.views import render_partial
    from apps.orders.models import Order

    from .segments import location_facets, places
    page, page_range, page_query = paginate(request, customers.select_related("channel_buyer"), 50)
    where = places(store, list(page.object_list))
    for customer in page.object_list:
        customer.place = where.get(customer.pk, {"where": "", "channels": []})
    used = set(Order.objects.filter(store=store, customer__isnull=False).values_list("channel", flat=True))
    used |= set(Customer.objects.filter(store=store, channel_buyer__isnull=False).values_list(
        "channel_buyer__platform", flat=True))
    names = dict(Order.CHANNELS)
    channel_choices = [(c, names.get(c, c)) for c, _ in Order.CHANNELS if c in used]
    tags = sorted({tag for value in Customer.objects.filter(store=store).exclude(tags="")
        .values_list("tags", flat=True) for tag in (t.strip() for t in value.split(",")) if tag})
    return render_partial(request, "merchant/customers.html", "merchant/_customers_list.html",
        _merchant_context(request, store, price_lists=__import__("apps.catalog.models", fromlist=["PriceList"]).PriceList.objects.filter(store=store, is_active=True), channel_choices=channel_choices,
        segment_channels=segment.get("channel", "").split(","), provinces=location_facets(store),
        include_pending=include_pending,
        customers=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        pending=pending, pending_count=pending.count(),
        segment=segment, segment_label=describe(segment), segment_query=urlencode(segment),
        tags=tags, audience=broadcast_audience(store, segment).count()))


@login_required(login_url="/login/")
def merchant_customer_new(request):
    """Add a customer by hand from the Pelanggan page (a dialog on that page). The same rule as the
    invoice form's quick add: a phone number already on file is that customer, never a duplicate."""
    from apps.orders.views import _require_roles, _staff_store
    from apps.tax.services import normalize_npwp

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    name = request.POST.get("name", "").strip()[:120]
    if not name:
        messages.error(request, "Nama pelanggan wajib diisi.")
        return redirect("merchant-customers")
    try:
        npwp = normalize_npwp(request.POST.get("npwp", ""))
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("merchant-customers")
    phone = request.POST.get("phone", "").strip()[:30]
    existing = Customer.objects.filter(store=store, phone=phone).first() if phone else None
    if existing:
        messages.info(request, f"Nomor {phone} sudah terdaftar atas nama {existing.name}; profil itu yang dibuka.")
        return redirect("merchant-customer-detail", customer_id=existing.id)
    from apps.catalog.models import PriceList
    days = request.POST.get("payment_terms_days", "").strip()
    list_id = request.POST.get("price_list", "").strip()
    customer = Customer.objects.create(store=store, name=name, phone=phone,
        payment_terms_days=int(days) if days.isdigit() else None,
        price_list=PriceList.objects.filter(store=store, pk=list_id).first() if list_id.isdigit() else None,
        email=request.POST.get("email", "").strip()[:254],
        company_name=request.POST.get("company_name", "").strip()[:160], npwp=npwp,
        address=request.POST.get("address", "").strip(), tags=request.POST.get("tags", "").strip()[:300],
        accepts_marketing=bool(request.POST.get("accepts_marketing")), status=Customer.STATUS_ACTIVE)
    AuditLog.objects.create(store=store, actor=request.user, action="customer.created",
        object_type="Customer", object_id=str(customer.pk), details={"name": name, "from": "pelanggan page"})
    messages.success(request, f"Pelanggan {customer.name} ditambahkan.")
    return redirect("merchant-customer-detail", customer_id=customer.id)


@login_required(login_url="/login/")
def merchant_customer_terms(request, customer_id):
    """Selling terms on a customer: payment terms (days) and the price list the invoice form applies."""
    from apps.catalog.models import PriceList
    from apps.orders.views import _require_roles, _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    customer = get_object_or_404(Customer, id=customer_id, store=store)
    days = request.POST.get("payment_terms_days", "").strip()
    customer.payment_terms_days = int(days) if days.isdigit() else None
    list_id = request.POST.get("price_list", "").strip()
    customer.price_list = PriceList.objects.filter(store=store, pk=list_id).first() if list_id.isdigit() else None
    customer.save(update_fields=["payment_terms_days", "price_list"])
    AuditLog.objects.create(store=store, actor=request.user, action="customer.terms_updated",
        object_type="Customer", object_id=str(customer.pk), details={
            "payment_terms_days": customer.payment_terms_days,
            "price_list": customer.price_list.name if customer.price_list else ""})
    messages.success(request, "Ketentuan penjualan disimpan.")
    return redirect("merchant-customer-detail", customer_id=customer.id)


@login_required(login_url="/login/")
def merchant_customer_tags(request, customer_id):
    from apps.orders.views import _require_roles, _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    customer = get_object_or_404(Customer, id=customer_id, store=store)
    tags = [tag.strip().replace(",", " ")[:40] for tag in request.POST.get("tags", "").split(",")]
    customer.tags = ", ".join(dict.fromkeys(tag for tag in tags if tag))[:300]
    customer.save(update_fields=["tags"])
    messages.success(request, "Tag disimpan.")
    return redirect("merchant-customer-detail", customer_id=customer.id)


@login_required(login_url="/login/")
def merchant_broadcasts(request):
    from apps.orders.views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, BROADCAST_ROLES)
    source = request.POST if request.method == "POST" else request.GET
    segment = parse(source)
    audience = broadcast_audience(store, segment).count()
    errors = {}
    subject = (request.POST.get("subject") or "").strip()
    body = (request.POST.get("body") or "").strip()
    if request.method == "POST":
        if not subject:
            errors["subject"] = "Isi judul email."
        if len(body) < 10:
            errors["body"] = "Isi pesan minimal 10 karakter."
        if not audience:
            errors["audience"] = "Tidak ada penerima: segmen ini tidak punya pelanggan yang setuju promo dan punya email."
        if not errors:
            broadcast = queue_broadcast(store, request.user, subject[:150], body, segment)
            AuditLog.objects.create(store=store, actor=request.user, action="broadcast.created",
                object_type="EmailBroadcast", object_id=str(broadcast.id),
                details={"label": subject[:60], "recipients": broadcast.recipients})
            messages.success(request, f"Broadcast masuk antrean untuk {broadcast.recipients} pelanggan. "
                "Email dikirim bertahap dalam beberapa menit.")
            return redirect("merchant-broadcasts")
    return render(request, "merchant/broadcasts.html", _merchant_context(request, store,
        title="Broadcast email", section="broadcasts", segment=segment,
        segment_label=describe(segment), segment_query=urlencode(segment), audience=audience,
        subject=subject, body=body, errors=errors,
        broadcasts=EmailBroadcast.objects.filter(store=store)[:30]))


@login_required(login_url="/login/")
def merchant_store_toggle(request):
    """Toggle storefront open/closed state."""
    from apps.orders.views import _require_roles, _staff_store
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager"})
    store.is_open = not store.is_open
    store.save(update_fields=["is_open"])
    from apps.shops.audit import log
    log(store, request.user, "store.opened" if store.is_open else "store.closed", store)
    messages.success(request, "Toko online dibuka kembali. Pembeli bisa memesan." if store.is_open
        else "Toko online ditutup sementara. Pembeli masih bisa melihat katalog, tetapi tidak bisa memesan.")
    return redirect("merchant-online-store")


def customer_unsubscribe(request, token):
    """Storefront: stop promotional email for this customer (one click, then confirm)."""
    data = read_unsubscribe(token)
    if not data or not request.store or data.get("s") != request.store.id:
        raise Http404
    customer = get_object_or_404(Customer, id=data.get("c"), store=request.store)
    done = False
    if request.method == "POST":
        customer.accepts_marketing = False
        customer.save(update_fields=["accepts_marketing"])
        done = True
    return render(request, "storefront/customer_unsubscribe.html", {"customer": customer, "done": done})


@login_required(login_url="/login/")
def merchant_customer_search(request):
    """JSON autocomplete: returns up to 20 customers matching q (name/phone/code/company)."""
    from apps.orders.views import _staff_store
    store = _staff_store(request)
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    qs = Customer.objects.filter(
        store=store, status__in=["active", "pending"]
    ).filter(
        Q(name__icontains=q) | Q(phone__icontains=q) |
        Q(customer_code__icontains=q) | Q(company_name__icontains=q) |
        Q(email__icontains=q)
    ).order_by("name")[:20]

    # Try to get last shipping address from most recent order
    from apps.orders.models import Order
    results = []
    for c in qs:
        last_order = Order.objects.filter(
            store=store, customer=c
        ).exclude(shipping_address="").exclude(shipping_address="-").order_by("-created_at").first()
        results.append({
            "id": str(c.id),
            "name": c.name,
            "price_list_id": str(c.price_list_id) if c.price_list_id else "",
            "payment_terms_days": c.payment_terms_days,
            "company_name": c.company_name,
            "phone": c.phone,
            "email": c.email,
            "npwp": c.npwp,
            "address": c.address or (last_order.shipping_address if last_order else ""),
            "label": f"{c.name}" + (f" ({c.company_name})" if c.company_name else "") +
                     (f" · {c.phone}" if c.phone else ""),
        })
    return JsonResponse({"results": results})


@login_required(login_url="/login/")
def merchant_customer_duplicates(request):
    """Customers that look like the same person, with one button to merge each group."""
    from apps.orders.views import _merchant_context, _require_roles, _staff_store

    from .customer_merge import MergeError, duplicate_groups, merge_customers

    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    if request.method == "POST":
        try:
            result = merge_customers(store, request.POST.get("primary"),
                request.POST.getlist("merge"), actor=request.user)
        except (MergeError, ValueError) as error:
            messages.error(request, str(error))
        else:
            moved = ", ".join(f"{count} {label.lower()}" for label, count in result["moved"].items())
            messages.success(request, f"{result['removed']} pelanggan digabung" + (f" ({moved} dipindahkan)." if moved else "."))
        return redirect("merchant-customer-duplicates")
    return render(request, "merchant/customer_duplicates.html", _merchant_context(request, store,
        groups=duplicate_groups(store)))


@login_required(login_url="/login/")
def merchant_customers_export(request):
    """The customers on screen (same filters), as Excel or CSV, with where they live and buy."""
    import csv
    import io

    from django.http import HttpResponse

    from apps.orders.views import _require_roles, _staff_store

    from .segments import places

    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    segment = parse(request.GET)
    pending_param = request.GET.get("pending")
    include_pending = pending_param == "1" or (pending_param is None and bool(
        segment.get("channel") or segment.get("location")))
    statuses = [Customer.STATUS_ACTIVE, Customer.STATUS_BLOCKED] + (
        [Customer.STATUS_PENDING] if include_pending else [])
    customers = list(customers_in(store, segment).filter(status__in=statuses)
        .select_related("channel_buyer").order_by("-created_at")[:20000])
    where = places(store, customers)
    header = ["Nama", "Perusahaan", "Telepon", "Email", "Alamat", "Kota", "Provinsi", "Beli di", "Pesanan",
        "Total belanja", "Terakhir belanja", "Tag", "Setuju promo", "Status"]
    rows = []
    for c in customers:
        w = where.get(c.pk, {"city": "", "province": "", "channels": []})
        rows.append([c.name, c.company_name, c.phone, c.email, c.address.replace("\n", " "), w["city"],
            w["province"], ", ".join(label for _, label in w["channels"]), c.order_count,
            float(c.total_spent), c.last_order_at.date().isoformat() if c.last_order_at else "", c.tags,
            "Ya" if c.accepts_marketing else "Tidak", c.get_status_display()])
    name = f"pelanggan-{store.slug}-{datetime.date.today():%Y%m%d}"
    if request.GET.get("format") == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(rows)
        response = HttpResponse("\ufeff" + out.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        return response
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Pelanggan"
    sheet.append(header)
    for cell in sheet[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1A66D9")
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCDEFGHIJKLMN", (26, 22, 16, 26, 40, 24, 22, 24, 9, 16, 16, 20, 12, 14), strict=False):
        sheet.column_dimensions[letter].width = width
    for cell in sheet["J"][1:]:
        cell.number_format = '"Rp"#,##0'
    sheet.append([])
    sheet.append([f"Filter: {describe(segment)}"])
    out = io.BytesIO()
    book.save(out)
    response = HttpResponse(out.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    return response
