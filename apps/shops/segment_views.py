"""Back office: customer segments, tags and email broadcasts; storefront: unsubscribe."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
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
    segment = parse(request.GET if request.method == "GET" else request.POST)
    customers = customers_in(store, segment).order_by("-created_at")
    if request.method == "POST" and request.POST.get("add_tag"):
        _require_roles(request, store, EDIT_ROLES)
        changed = add_tag(customers, request.POST["add_tag"])
        messages.success(request, f"Tag ditambahkan ke {changed} pelanggan.")
        return redirect(f"{request.path}?{urlencode(segment)}")
    page, page_range, page_query = paginate(request, customers, 50)
    tags = sorted({tag for value in Customer.objects.filter(store=store).exclude(tags="")
        .values_list("tags", flat=True) for tag in (t.strip() for t in value.split(",")) if tag})
    return render(request, "merchant/customers.html", _merchant_context(request, store,
        customers=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
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
