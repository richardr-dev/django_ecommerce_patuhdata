"""Pesanan pembelian: order from a supplier, receive in parts, then bill from the receipts."""
import datetime
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalog.models import Warehouse
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import idempotency, purchasing
from .models import PurchaseOrder, Supplier

LOGIN = "/login/"
ORDER_ROLES = permissions.roles("purchase.order")
VIEW_ROLES = ORDER_ROLES | permissions.roles("stock.receive") | {"viewer"}


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


@login_required(login_url=LOGIN)
def merchant_purchase_orders(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    show = request.GET.get("show", "open")
    rows = PurchaseOrder.objects.filter(store=store).select_related("supplier").prefetch_related("lines")
    if show == "open":
        rows = rows.filter(status__in=[PurchaseOrder.OPEN, PurchaseOrder.PARTIAL])
    rows = list(rows[:300])
    return render(request, "merchant/purchase_orders.html", _merchant_context(request, store, rows=rows, show=show,
        waiting=purchasing.waiting_value(store), late=purchasing.overdue(store).count(),
        today=timezone.localdate(), can_order=permissions.can(request.user, store, "purchase.order")))


@login_required(login_url=LOGIN)
def merchant_purchase_order_new(request):
    store = _staff_store(request)
    _require_roles(request, store, ORDER_ROLES)
    suppliers = Supplier.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "purchase_order", key)
        if earlier is not None:
            messages.info(request, "Pesanan ini sudah disimpan.")
            return redirect("merchant-purchase-order", po_id=earlier) if earlier else redirect("merchant-purchase-orders")
        try:
            supplier = suppliers.filter(pk=request.POST.get("supplier_id") or 0).first()
            if not supplier and request.POST.get("supplier_name", "").strip():
                supplier, _ = Supplier.objects.get_or_create(store=store, name=request.POST["supplier_name"].strip()[:160])
            if not supplier:
                raise purchasing.PurchaseError("Pilih atau isi nama supplier.")
            lines = [{"sku": sku, "qty": _number(qty), "unit_cost": _number(cost)} for sku, qty, cost in zip(
                request.POST.getlist("sku[]"), request.POST.getlist("qty[]"), request.POST.getlist("unit_cost[]"),
                strict=False) if sku.strip()]
            expected = request.POST.get("expected_on")
            po = purchasing.create_po(store, supplier=supplier, lines=lines, actor=request.user,
                order_date=datetime.date.fromisoformat(request.POST.get("order_date") or timezone.localdate().isoformat()),
                expected_on=datetime.date.fromisoformat(expected) if expected else None,
                notes=request.POST.get("notes", "")[:300])
        except (purchasing.PurchaseError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "purchase_order", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "purchase_order", key, po.pk)
            audit_log(store, request.user, "purchase_order.created", po, supplier=supplier.name, value=po.value)
            messages.success(request, f"Pesanan {po.number} dibuat. Catat barangnya saat tiba lewat \"Terima barang\".")
            return redirect("merchant-purchase-order", po_id=po.pk)
    return render(request, "merchant/purchase_order_new.html", _merchant_context(request, store, suppliers=suppliers,
        today=timezone.localdate().isoformat(), idem_key=idempotency.new_key()))


@login_required(login_url=LOGIN)
def merchant_purchase_order(request, po_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        po = get_object_or_404(PurchaseOrder.objects.select_related("supplier"), pk=_uuid.UUID(str(po_id)), store=store)
    except ValueError:
        raise Http404 from None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "close":
                _require_roles(request, store, ORDER_ROLES)
                purchasing.close_po(po, actor=request.user)
                audit_log(store, request.user, "purchase_order.closed", po)
                messages.success(request, "Sisa pesanan ditutup.")
            elif action == "receive":
                _require_roles(request, store, permissions.roles("stock.receive"))
                key = request.POST.get("idem", "")
                earlier = idempotency.claim(store, "po_receive", key)
                if earlier is not None:
                    messages.info(request, "Penerimaan ini sudah disimpan.")
                    return redirect("merchant-purchase-order", po_id=po.pk)
                quantities = {line.pk: int(_number(request.POST.get(f"qty_{line.pk}"))) for line in po.lines.all()}
                warehouse = Warehouse.objects.filter(store=store, pk=request.POST.get("warehouse") or 0).first()
                try:
                    receipt = purchasing.receive_po(po, quantities=quantities, warehouse=warehouse, actor=request.user,
                        received_on=datetime.date.fromisoformat(request.POST.get("received_on") or timezone.localdate().isoformat()),
                        number=request.POST.get("number", "")[:40])
                except Exception:
                    idempotency.release(store, "po_receive", key)
                    raise
                idempotency.finish(store, "po_receive", key, receipt.pk)
                audit_log(store, request.user, "purchase_order.received", po, receipt=receipt.pk)
                messages.success(request, "Barang diterima dan masuk stok. Buat tagihannya dari halaman penerimaan.")
                return redirect("merchant-goods-receipt", receipt_id=receipt.pk)
        except (purchasing.PurchaseError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-purchase-order", po_id=po.pk)
    lines = list(po.lines.all())
    for line in lines:
        line.billed = line.billed_quantity
    return render(request, "merchant/purchase_order_detail.html", _merchant_context(request, store, po=po, lines=lines,
        receipts=po.receipts.select_related("bill"), audit=audit_trail(store, po), today=timezone.localdate().isoformat(),
        warehouses=Warehouse.objects.filter(store=store), idem_key=idempotency.new_key(),
        can_order=permissions.can(request.user, store, "purchase.order"),
        can_receive=permissions.can(request.user, store, "stock.receive")))
