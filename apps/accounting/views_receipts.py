"""Penerimaan barang: goods that arrived before the supplier's bill (GRNI)."""
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
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail
from apps.shops.permissions import roles

from . import idempotency, receipts
from .ledger import entries_for
from .models import GoodsReceipt, Supplier

# Whoever receives goods does not have to be whoever books the bill: the warehouse records what
# arrived, finance turns it into the supplier's bill.
RECEIVE_ROLES = roles("stock.receive")
BILL_ROLES = roles("books.edit")
VIEW_ROLES = RECEIVE_ROLES | {"viewer"}
LOGIN = "/merchant/login/"


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


@login_required(login_url=LOGIN)
def merchant_goods_receipts(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    show = request.GET.get("show", "waiting")
    rows = GoodsReceipt.objects.filter(store=store).select_related("supplier").prefetch_related("lines")
    if show == "waiting":
        rows = rows.filter(bill__isnull=True)
    rows = list(rows[:300])
    waiting = receipts.unbilled(store).prefetch_related("lines")
    return render(request, "merchant/goods_receipts.html", _merchant_context(request, store, rows=rows, show=show,
        waiting_count=len(waiting), waiting_value=sum((r.value for r in waiting), Decimal(0)),
        can_receive=True))


@login_required(login_url=LOGIN)
def merchant_goods_receipt_new(request):
    store = _staff_store(request)
    _require_roles(request, store, RECEIVE_ROLES)
    suppliers = Supplier.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "goods_receipt", key)
        if earlier is not None:
            messages.info(request, "Penerimaan ini sudah disimpan.")
            return redirect("merchant-goods-receipt", receipt_id=earlier) if earlier else redirect("merchant-goods-receipts")
        try:
            supplier = suppliers.filter(pk=request.POST.get("supplier_id") or 0).first()
            if not supplier and request.POST.get("supplier_name", "").strip():
                supplier, _ = Supplier.objects.get_or_create(store=store, name=request.POST["supplier_name"].strip()[:160])
            if not supplier:
                raise receipts.ReceiptError("Pilih atau isi nama supplier.")
            lines = []
            for sku, qty, cost in zip(request.POST.getlist("sku[]"), request.POST.getlist("qty[]"),
                    request.POST.getlist("unit_cost[]"), strict=False):
                if sku.strip():
                    lines.append({"sku": sku, "qty": _number(qty), "unit_cost": _number(cost)})
            warehouse = Warehouse.objects.filter(store=store, pk=request.POST.get("warehouse") or 0).first()
            receipt = receipts.create_receipt(store, supplier=supplier, lines=lines,
                received_on=datetime.date.fromisoformat(request.POST.get("received_on") or timezone.localdate().isoformat()),
                number=request.POST.get("number", "")[:40], warehouse=warehouse,
                notes=request.POST.get("notes", "")[:300], actor=request.user)
        except (receipts.ReceiptError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "goods_receipt", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "goods_receipt", key, receipt.pk)
            audit_log(store, request.user, "receipt.created", receipt, value=receipt.value, supplier=supplier.name)
            messages.success(request, "Barang diterima dan masuk stok. Utangnya dicatat di GRNI sampai tagihan datang.")
            return redirect("merchant-goods-receipt", receipt_id=receipt.pk)
    return render(request, "merchant/goods_receipt_new.html", _merchant_context(request, store, suppliers=suppliers,
        warehouses=Warehouse.objects.filter(store=store), today=timezone.localdate().isoformat(),
        idem_key=idempotency.new_key()))


@login_required(login_url=LOGIN)
def merchant_goods_receipt(request, receipt_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        receipt = get_object_or_404(GoodsReceipt.objects.select_related("supplier", "bill"), pk=_uuid.UUID(str(receipt_id)),
            store=store)
    except ValueError:
        raise Http404 from None
    if request.method == "POST":
        _require_roles(request, store, BILL_ROLES)
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "receipt_bill", key)
        if earlier is not None:
            messages.info(request, "Tagihan untuk penerimaan ini sudah dibuat.")
            return redirect("merchant-goods-receipt", receipt_id=receipt.pk)
        try:
            prices = {}
            for line in receipt.lines.all():
                raw = request.POST.get(f"price_{line.pk}", "")
                if raw.strip():
                    prices[line.pk] = _number(raw)
            due = request.POST.get("due_date")
            bill = receipts.bill_receipt(receipt, number=request.POST.get("number", "")[:40],
                bill_date=datetime.date.fromisoformat(request.POST.get("bill_date") or timezone.localdate().isoformat()),
                due_date=datetime.date.fromisoformat(due) if due else None, tax=_number(request.POST.get("tax")),
                prices=prices, actor=request.user)
        except (receipts.ReceiptError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "receipt_bill", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "receipt_bill", key, bill.pk)
            audit_log(store, request.user, "receipt.billed", receipt, bill=bill.pk)
            messages.success(request, "Tagihan dibuat dari penerimaan. GRNI dipindah ke Utang usaha.")
            return redirect("merchant-bill-detail", bill_id=bill.pk)
        return redirect("merchant-goods-receipt", receipt_id=receipt.pk)
    journal = entries_for(store, [("goods_receipt", receipt.pk)] + ([("bill", receipt.bill_id)] if receipt.bill_id else []))
    return render(request, "merchant/goods_receipt_detail.html", _merchant_context(request, store, receipt=receipt,
        lines=list(receipt.lines.select_related("product", "variant")), journal=journal,
        audit=audit_trail(store, receipt), today=timezone.localdate().isoformat(),
        can_bill=_can(request, store, BILL_ROLES), idem_key=idempotency.new_key()))


def _can(request, store, roles):
    return store.owner_id == request.user.id or store.staff_members.filter(user=request.user, role__in=roles).exists()
