"""Permintaan stok antar gudang / outlet: request, approve, send, receive."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounting import idempotency
from apps.catalog.models import Warehouse
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import stock_requests
from .models import StockRequest
from .views import _merchant_context, _require_roles, _staff_store

LOGIN = "/login/"
ALL_ROLES = (permissions.roles("stock.request") | permissions.roles("stock.request_approve")
    | permissions.roles("stock.send") | {"viewer"})


def _int(raw):
    return int(Decimal((raw or "0").strip() or "0"))


@login_required(login_url=LOGIN)
def merchant_stock_requests(request):
    store = _staff_store(request)
    _require_roles(request, store, ALL_ROLES)
    show = request.GET.get("show", "open")
    rows = StockRequest.objects.filter(store=store).select_related("from_warehouse", "to_warehouse", "requested_by")
    if show == "open":
        rows = rows.filter(status__in=[StockRequest.REQUESTED, StockRequest.APPROVED, StockRequest.SENT])
    return render(request, "merchant/stock_requests.html", _merchant_context(request, store, rows=rows[:200], show=show,
        can_request=permissions.can(request.user, store, "stock.request")))


@login_required(login_url=LOGIN)
def merchant_stock_request_new(request):
    store = _staff_store(request)
    _require_roles(request, store, permissions.roles("stock.request"))
    warehouses = list(Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id"))
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "stock_request", key)
        if earlier is not None:
            messages.info(request, "Permintaan ini sudah disimpan.")
            return redirect("merchant-stock-request", request_id=earlier) if earlier else redirect("merchant-stock-requests")
        try:
            source = next((w for w in warehouses if str(w.pk) == request.POST.get("from_warehouse")), None)
            target = next((w for w in warehouses if str(w.pk) == request.POST.get("to_warehouse")), None)
            if not source or not target:
                raise stock_requests.RequestError("Pilih gudang asal dan tujuan.")
            lines = [{"sku": sku, "qty": Decimal((qty or "0").replace(".", "").replace(",", ".") or "0")}
                for sku, qty in zip(request.POST.getlist("sku[]"), request.POST.getlist("qty[]"), strict=False) if sku.strip()]
            created = stock_requests.create_request(store, from_warehouse=source, to_warehouse=target, lines=lines,
                notes=request.POST.get("notes", ""), actor=request.user)
        except (stock_requests.RequestError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "stock_request", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "stock_request", key, created.pk)
            audit_log(store, request.user, "stock_request.created", created, to=target.name)
            messages.success(request, f"Permintaan {created.number} diajukan. Admin atau manajer menyetujui, lalu gudang mengirim.")
            return redirect("merchant-stock-request", request_id=created.pk)
    return render(request, "merchant/stock_request_new.html", _merchant_context(request, store, warehouses=warehouses,
        idem_key=idempotency.new_key()))


@login_required(login_url=LOGIN)
def merchant_stock_request(request, request_id):
    store = _staff_store(request)
    _require_roles(request, store, ALL_ROLES)
    try:
        doc = get_object_or_404(StockRequest.objects.select_related("from_warehouse", "to_warehouse", "requested_by",
            "approved_by", "sent_by", "received_by"), pk=request_id, store=store)
    except ValueError:
        raise Http404 from None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            lines = list(doc.lines.all())
            if action in ("approve", "reject"):
                stock_requests.approve(doc, actor=request.user, reject=action == "reject")
                messages.success(request, "Permintaan disetujui." if action == "approve" else "Permintaan ditolak.")
            elif action == "cancel":
                _require_roles(request, store, permissions.roles("stock.request"))
                stock_requests.cancel(doc)
                messages.success(request, "Permintaan dibatalkan.")
            elif action == "send":
                _require_roles(request, store, permissions.roles("stock.send"))
                stock_requests.send(doc, actor=request.user, quantities={l.pk: _int(request.POST.get(f"send_{l.pk}")) for l in lines})
                messages.success(request, "Barang dikirim. Stok keluar dari gudang asal dan menunggu diterima.")
            elif action == "receive":
                _require_roles(request, store, permissions.roles("stock.request"))
                stock_requests.receive(doc, actor=request.user, quantities={l.pk: _int(request.POST.get(f"recv_{l.pk}")) for l in lines})
                messages.success(request, "Barang diterima dan masuk stok gudang tujuan.")
            audit_log(store, request.user, f"stock_request.{action}", doc)
        except (stock_requests.RequestError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-stock-request", request_id=doc.pk)
    return render(request, "merchant/stock_request_detail.html", _merchant_context(request, store, doc=doc,
        lines=list(doc.lines.select_related("product", "variant")), audit=audit_trail(store, doc),
        can_request=permissions.can(request.user, store, "stock.request"),
        can_approve=permissions.can(request.user, store, "stock.request_approve"),
        can_send=permissions.can(request.user, store, "stock.send")))
