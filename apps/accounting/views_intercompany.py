"""Transfer antar brand (intercompany). Both brands' books change, so the person must be an owner
or admin of both."""
import datetime
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.audit import log as audit_log

from . import idempotency, intercompany
from .ledger import entries_for
from .models import IntercompanyTransfer

LOGIN = "/merchant/login/"
VIEW_ROLES = {"admin", "manager", "finance", "viewer"}


def _admin_of(user, store):
    return store.owner_id == user.id or store.staff_members.filter(user=user, role="admin").exists()


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


@login_required(login_url=LOGIN)
def merchant_intercompany(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    rows = IntercompanyTransfer.objects.filter(Q(from_store=store) | Q(to_store=store)).select_related(
        "from_store", "to_store").prefetch_related("lines")[:200]
    receivable, payable = intercompany.group_balances(store.organization) if store.organization_id else (0, 0)
    from .ledger import INTERCO_PAYABLE, INTERCO_RECEIVABLE, account_balances
    mine = {a.code: a.balance for a in account_balances(store)}
    return render(request, "merchant/intercompany.html", _merchant_context(request, store, rows=rows,
        owed_to_me=mine.get(INTERCO_RECEIVABLE, 0), i_owe=mine.get(INTERCO_PAYABLE, 0),
        group_receivable=receivable, group_payable=payable, others=intercompany.counterparts(store).count(),
        can_create=_admin_of(request.user, store)))


@login_required(login_url=LOGIN)
def merchant_intercompany_new(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin"})
    targets = [s for s in intercompany.counterparts(store) if _admin_of(request.user, s)]
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "intercompany", key)
        if earlier is not None:
            messages.info(request, "Transfer ini sudah disimpan.")
            return redirect("merchant-intercompany-detail", transfer_id=earlier) if earlier else redirect("merchant-intercompany")
        try:
            target = next((s for s in targets if str(s.pk) == request.POST.get("to_store")), None)
            if not target:
                raise intercompany.TransferError("Pilih brand tujuan. Anda harus admin di brand itu juga.")
            lines = [{"sku": sku, "qty": _number(qty), "unit_price": _number(price)} for sku, qty, price in zip(
                request.POST.getlist("sku[]"), request.POST.getlist("qty[]"), request.POST.getlist("unit_price[]"),
                strict=False) if sku.strip()]
            transfer = intercompany.create_transfer(store, target, lines=lines,
                transfer_date=datetime.date.fromisoformat(request.POST.get("transfer_date") or timezone.localdate().isoformat()),
                number=request.POST.get("number", "")[:40], notes=request.POST.get("notes", "")[:300], actor=request.user)
        except (intercompany.TransferError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "intercompany", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "intercompany", key, transfer.pk)
            audit_log(store, request.user, "intercompany.created", transfer, to=target.name, total=transfer.price_total)
            messages.success(request, f"Barang dikirim ke {target.name}. Buku kedua brand sudah dicatat.")
            return redirect("merchant-intercompany-detail", transfer_id=transfer.pk)
    return render(request, "merchant/intercompany_new.html", _merchant_context(request, store, targets=targets,
        today=timezone.localdate().isoformat(), idem_key=idempotency.new_key()))


@login_required(login_url=LOGIN)
def merchant_intercompany_detail(request, transfer_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        transfer = get_object_or_404(IntercompanyTransfer.objects.select_related("from_store", "to_store"),
            Q(from_store=store) | Q(to_store=store), pk=_uuid.UUID(str(transfer_id)))
    except ValueError:
        raise Http404 from None
    seller = transfer.from_store_id == store.pk
    journal = entries_for(store, [("ic_out" if seller else "ic_in", transfer.pk)])
    return render(request, "merchant/intercompany_detail.html", _merchant_context(request, store, transfer=transfer,
        lines=list(transfer.lines.all()), journal=journal, seller=seller))
