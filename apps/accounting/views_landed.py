"""Biaya perolehan (landed cost) pages. Logic lives in accounting/landed_costs.py."""
import datetime
import uuid as _uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import idempotency, landed_costs
from .ledger import LedgerError, entries_for
from .models import Bill, GoodsReceipt, LandedCost

LOGIN = "/login/"
ALLOCATE_ROLES = permissions.roles("purchase.landed_cost")
VIEW_ROLES = ALLOCATE_ROLES | permissions.roles("purchase.order") | permissions.roles("books.view") | {"viewer"}


@login_required(login_url=LOGIN)
def merchant_landed_costs(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, ALLOCATE_ROLES)
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "landed_cost", key)
        if earlier is not None:
            messages.info(request, "Alokasi ini sudah disimpan.")
            return redirect("merchant-landed-cost", cost_id=earlier) if earlier else redirect("merchant-landed-costs")
        try:
            bill = Bill.objects.filter(store=store, pk=request.POST.get("bill") or None).first() if _is_uuid(
                request.POST.get("bill")) else None
            if bill is None:
                raise landed_costs.LandedCostError("Pilih tagihan ongkos angkut / bea masuk.")
            receipts = GoodsReceipt.objects.filter(store=store, pk__in=[v for v in request.POST.getlist("receipt")
                if _is_uuid(v)])
            cost = landed_costs.allocate(store, bill=bill, receipts=list(receipts), actor=request.user,
                method=request.POST.get("method") or LandedCost.BY_VALUE, notes=request.POST.get("notes", "")[:300],
                date=datetime.date.fromisoformat(request.POST.get("date") or timezone.localdate().isoformat()))
        except (landed_costs.LandedCostError, LedgerError, ValueError) as error:
            idempotency.release(store, "landed_cost", key)
            messages.error(request, f"Gagal: {error}")
            return redirect("merchant-landed-costs")
        idempotency.finish(store, "landed_cost", key, cost.pk)
        audit_log(store, request.user, "landed_cost.allocated", cost, bill=bill.number, amount=cost.amount)
        messages.success(request, f"{cost.number}: {cost.amount:,.0f} masuk ke harga pokok barang.".replace(",", "."))
        return redirect("merchant-landed-cost", cost_id=cost.pk)
    return render(request, "merchant/landed_costs.html", _merchant_context(request, store,
        rows=LandedCost.objects.filter(store=store).select_related("bill__supplier")[:300],
        bills=[(bill, landed_costs.unallocated(bill)) for bill in landed_costs.open_bills(store)],
        receipts=GoodsReceipt.objects.filter(store=store).select_related("supplier").order_by("-received_on")[:200],
        waiting=landed_costs.waiting(store), today=timezone.localdate().isoformat(), idem_key=idempotency.new_key(),
        preselect=request.GET.get("bill", ""), methods=LandedCost.METHODS,
        can_allocate=permissions.can(request.user, store, "purchase.landed_cost")))


def _is_uuid(value):
    try:
        _uuid.UUID(str(value))
    except ValueError:
        return False
    return True


@login_required(login_url=LOGIN)
def merchant_landed_cost(request, cost_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    cost = get_object_or_404(LandedCost.objects.select_related("bill__supplier"), pk=cost_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, ALLOCATE_ROLES)
        if request.POST.get("action") != "void":
            raise Http404
        try:
            landed_costs.void(cost, actor=request.user)
        except (landed_costs.LandedCostError, LedgerError) as error:
            messages.error(request, str(error))
        else:
            audit_log(store, request.user, "landed_cost.voided", cost)
            messages.success(request, f"{cost.number} dibatalkan; harga pokok barang dikembalikan.")
        return redirect("merchant-landed-cost", cost_id=cost.pk)
    return render(request, "merchant/landed_cost_detail.html", _merchant_context(request, store, cost=cost,
        lines=cost.lines.select_related("product", "variant", "receipt_line__receipt"),
        receipts=cost.receipts.select_related("supplier"), journal=entries_for(store, [("landed_cost", cost.pk)]),
        audit=audit_trail(store, cost), can_allocate=permissions.can(request.user, store, "purchase.landed_cost")))
