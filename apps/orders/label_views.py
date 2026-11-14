"""Unggah label resi: a PDF of marketplace shipping labels in, the resi and courier saved on each order out.

Two steps so nothing is saved by surprise: the upload only reads and previews (nothing is stored, not even
the file); saving takes what the seller ticked and checks every value again on the server."""
import re
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.shops.models import AuditLog
from apps.shops.permissions import roles

from .models import Order
from .resi_labels import CLOSED_STATUSES, COURIERS, LabelError, match_labels, read_labels
from .views import _apply_tracking, _merchant_context, _require_roles, _staff_store

RESI_ROLES = roles("stock.fulfil")           # the same people who may type a resi by hand
COURIER_CHOICES = sorted(COURIERS.items(), key=lambda item: item[1].lower())
STATE_LABELS = {"ok": ("Siap disimpan", "paid"), "same": ("Sudah tersimpan", "draft"),
    "differs": ("Resi berbeda", "pending"), "closed": ("Tidak bisa diisi", "void"),
    "no_order": ("Pesanan tidak ketemu", "overdue"), "no_resi": ("Resi tidak terbaca", "overdue"),
    "no_text": ("Tidak ada teks", "overdue"), "duplicate": ("Label ganda", "draft")}


def _summary(rows):
    counts = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return {"total": len(rows), "ready": counts.get("ok", 0), "check": counts.get("differs", 0),
        "done": counts.get("same", 0) + counts.get("duplicate", 0),
        "problems": sum(counts.get(key, 0) for key in ("closed", "no_order", "no_resi", "no_text"))}


@login_required(login_url="/login/")
def merchant_resi_labels(request):
    store = _staff_store(request)
    _require_roles(request, store, RESI_ROLES)
    context = {"title": "Unggah label resi", "courier_choices": COURIER_CHOICES, "state_labels": STATE_LABELS}
    if request.method == "POST" and request.POST.get("action") == "save":
        return _save(request, store)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Pilih file PDF label dulu.")
        else:
            try:
                labels = read_labels(upload)
            except LabelError as error:
                messages.error(request, str(error))
            else:
                rows = match_labels(store, labels)
                for row in rows:
                    row.label, row.pill = STATE_LABELS[row.state]
                context.update(rows=rows, summary=_summary(rows), file_name=upload.name)
    return render(request, "merchant/resi_labels.html", _merchant_context(request, store, **context))


def _save(request, store):
    try:
        count = max(0, min(int(request.POST.get("n", "0")), 500))
    except ValueError:
        count = 0
    saved, skipped = 0, []
    for index in range(count):
        if not request.POST.get(f"use_{index}"):
            continue
        resi = request.POST.get(f"resi_{index}", "").strip().upper()
        code = request.POST.get(f"courier_{index}", "").strip().lower()
        try:
            order_id = uuid.UUID(request.POST.get(f"order_{index}", ""))
        except ValueError:
            continue
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{5,39}", resi):
            skipped.append(f"{resi or 'label'}: nomor resi tidak valid")
            continue
        if code not in COURIERS:
            skipped.append(f"{resi}: pilih ekspedisinya")
            continue
        with transaction.atomic():
            order = Order.objects.select_for_update().filter(id=order_id, store=store).first()
            if order is None or order.status in CLOSED_STATUSES:
                skipped.append(f"{resi}: pesanan tidak bisa diisi resi")
                continue
            error = _apply_tracking(order, {"tracking_number": resi, "courier_code": code})
            if error:
                skipped.append(f"{resi}: {error}")
                continue
            if not order.shipping_service:
                Order.objects.filter(pk=order.pk).update(shipping_service=COURIERS[code])
            AuditLog.objects.create(store=store, actor=request.user, action="order.tracking_updated",
                object_type="Order", object_id=str(order.id), details={"tracking_number": resi,
                    "courier_code": code, "source": "label_pdf"})
            saved += 1
    if saved:
        messages.success(request, f"{saved} resi disimpan ke pesanan.")
    for note in skipped[:10]:
        messages.warning(request, note)
    if not saved and not skipped:
        messages.info(request, "Tidak ada label yang dipilih.")
    # Marketplace orders are hidden on Pesanan unless asked for: open it showing them, so the result is visible.
    return redirect(reverse("merchant-resi-labels") if skipped and not saved
        else reverse("merchant-orders") + "?tab=all&sumber=semua")
