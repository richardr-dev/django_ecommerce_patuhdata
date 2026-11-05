"""Log aktivitas: who did what in this store, from the audit log every change already writes.
Owners and store admins only; read-only."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from .models import AuditLog

GROUPS = {"order": "Pesanan", "refund": "Refund", "payment": "Pembayaran", "products": "Produk",
    "variants": "Produk", "inventory": "Stok", "staff": "Staf", "store": "Toko",
    "billing": "Langganan", "channel": "Marketplace", "campaign_link": "Promosi", "whatsapp": "WhatsApp", "expense": "Keuangan",
    "coupons": "Promosi", "gift_card": "Promosi", "campaigns": "Promosi", "flash-sales": "Promosi"}
VERBS = {"created": "membuat", "updated": "mengubah", "invited": "mengundang", "removed": "menghapus",
    "role_changed": "mengubah peran", "status_updated": "mengubah status", "shipped": "mengirim",
    "shipment_booked": "memesan kurir", "tracking_updated": "mengisi resi", "adjusted": "menyesuaikan",
    "manual_created": "membuat pesanan manual", "requested": "mengajukan", "imported": "mengimpor",
    "transfer_confirmed": "mengonfirmasi transfer", "settings_updated": "mengubah pengaturan",
    "template_applied": "menerapkan template", "tracking_updated_store": "mengubah pelacakan",
    "subscription_paid": "membayar langganan", "checkout_started": "membuka pembayaran langganan",
    "csv_imported": "mengimpor CSV", "orders_imported": "mengimpor pesanan"}


def describe(entry):
    group, _, verb = entry.action.partition(".")
    label = entry.details.get("label") if isinstance(entry.details, dict) else ""
    return {"group": GROUPS.get(group, group.replace("_", " ").replace("-", " ").title()),
        "verb": VERBS.get(verb, verb.replace("_", " ")), "label": label or ""}


@login_required(login_url="/merchant/login/")
def merchant_activity(request):
    from apps.catalog.views import paginate
    from apps.orders.views import _merchant_context, _staff_store

    store = _staff_store(request)
    if store.owner_id != request.user.id and not store.staff_members.filter(user=request.user,
            role="admin").exists():
        raise Http404
    entries = AuditLog.objects.filter(store=store).select_related("actor").order_by("-created_at", "-id")
    group = request.GET.get("group", "")
    prefixes = [key for key, value in GROUPS.items() if value == group]
    if prefixes:
        from django.db.models import Q
        query = Q()
        for prefix in prefixes:
            query |= Q(action__startswith=f"{prefix}.")
        entries = entries.filter(query)
    actor = request.GET.get("actor", "")
    if actor.isdigit():
        entries = entries.filter(actor_id=int(actor))
    page, page_range, page_query = paginate(request, entries, 50)
    rows = [{"entry": entry, **describe(entry)} for entry in page.object_list]
    people = {entry.actor_id: entry.actor for entry in
        AuditLog.objects.filter(store=store, actor__isnull=False).select_related("actor")
        .order_by("-id")[:500]}
    return render(request, "merchant/activity.html", _merchant_context(request, store,
        title="Log aktivitas", section="activity", rows=rows, page_obj=page, page_range=page_range,
        page_query=page_query, groups=sorted(set(GROUPS.values())), group=group,
        people=list(people.values()), actor=actor))
