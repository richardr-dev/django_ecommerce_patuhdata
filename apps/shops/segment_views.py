"""Back office: customer segments, tags and email broadcasts; storefront: unsubscribe."""
import json
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

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

VIEW_ROLES = {"admin", "manager", "ops", "customer_service", "viewer"}
EDIT_ROLES = {"admin", "manager", "customer_service"}
BROADCAST_ROLES = {"admin", "manager"}


@login_required(login_url="/merchant/login/")
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
    customers = customers_in(store, segment).filter(
        status__in=[Customer.STATUS_ACTIVE, Customer.STATUS_BLOCKED]
    ).order_by("-created_at")
    pending = Customer.objects.filter(store=store, status=Customer.STATUS_PENDING).select_related(
        "channel_buyer").order_by("-created_at")

    page, page_range, page_query = paginate(request, customers, 50)
    tags = sorted({tag for value in Customer.objects.filter(store=store).exclude(tags="")
        .values_list("tags", flat=True) for tag in (t.strip() for t in value.split(",")) if tag})
    return render(request, "merchant/customers.html", _merchant_context(request, store,
        customers=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        pending=pending, pending_count=pending.count(),
        segment=segment, segment_label=describe(segment), segment_query=urlencode(segment),
        tags=tags, audience=broadcast_audience(store, segment).count()))


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
def merchant_store_toggle(request):
    """Toggle storefront open/closed state."""
    from apps.orders.views import _require_roles, _staff_store
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager"})
    store.is_open = not store.is_open
    store.save(update_fields=["is_open"])
    status = "dibuka" if store.is_open else "ditutup"
    messages.success(request, f"Toko {status}.")
    return redirect(request.META.get("HTTP_REFERER", "/merchant/"))


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


@login_required(login_url="/merchant/login/")
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
            "company_name": c.company_name,
            "phone": c.phone,
            "email": c.email,
            "npwp": c.npwp,
            "address": c.address or (last_order.shipping_address if last_order else ""),
            "label": f"{c.name}" + (f" ({c.company_name})" if c.company_name else "") +
                     (f" · {c.phone}" if c.phone else ""),
        })
    return JsonResponse({"results": results})


@login_required(login_url="/merchant/login/")
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
