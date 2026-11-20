"""Selisih & klaim marketplace page (/books/payouts/selisih/)."""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.orders.views import _merchant_context, _require_roles, _staff_store

from . import idempotency
from . import marketplace_adjustments as adj_service
from .bank import bank_accounts
from .ledger import entries_for
from .models import Account, MarketplaceAdjustment
from .payouts import payout_providers
from .views import EDIT_ROLES, VIEW_ROLES
from .views_books import _can_edit

SCOPE = "mp_adjustment"


def _date(text, default=None):
    try:
        return datetime.date.fromisoformat(text) if text else default
    except ValueError:
        return None


@login_required(login_url="/login/")
def merchant_marketplace_adjustments(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    posted = {}
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action", "create")
        if action == "create":
            posted = request.POST
            key = request.POST.get("idem", "")
            if idempotency.claim(store, SCOPE, key) is not None:
                messages.info(request, "Selisih ini sudah dicatat.")
                return redirect("merchant-marketplace-adjustments")
            account = Account.objects.filter(store=store, pk=request.POST.get("account") or 0).first()
            try:
                adj = adj_service.record(store, provider=request.POST.get("provider", ""),
                    kind=request.POST.get("kind", ""), account=account, amount=request.POST.get("amount", ""),
                    date=_date(request.POST.get("date")), description=request.POST.get("description", ""),
                    reference=request.POST.get("reference", ""), actor=request.user)
            except adj_service.AdjustmentError as error:
                idempotency.release(store, SCOPE, key)
                messages.error(request, str(error))
            else:
                idempotency.finish(store, SCOPE, key, adj.pk)
                messages.success(request, "Selisih marketplace dicatat dan sudah masuk buku besar.")
                return redirect("merchant-marketplace-adjustments")
        else:
            adj = get_object_or_404(MarketplaceAdjustment, pk=request.POST.get("id") or 0, store=store)
            try:
                if action == "settle":
                    when = _date(request.POST.get("settled_on"), datetime.date.today())
                    if when is None:
                        raise adj_service.AdjustmentError("Isi tanggal pelunasan.")
                    bank = next((b for b in bank_accounts(store) if str(b.pk) == request.POST.get("bank_account")), None)
                    adj_service.settle(adj, date=when, bank_account=bank, note=request.POST.get("note", ""),
                        actor=request.user)
                    messages.success(request, "Selisih dilunasi.")
                elif action == "unsettle":
                    adj_service.unsettle(adj, actor=request.user)
                    messages.success(request, "Pelunasan dibatalkan; selisih terbuka lagi.")
                elif action == "void":
                    adj_service.void(adj, actor=request.user)
                    messages.success(request, "Selisih dibatalkan; jurnalnya dibalik.")
            except adj_service.AdjustmentError as error:
                messages.error(request, str(error))
            return redirect("merchant-marketplace-adjustments")

    labels = dict(payout_providers())
    rows = list(MarketplaceAdjustment.objects.filter(store=store).select_related("account", "settled_bank")[:200])
    for row in rows:
        row.label = labels.get(row.provider, row.provider)
    open_rows = [r for r in rows if r.is_open]
    journal = entries_for(store, [(adj_service.SOURCE, r.pk) for r in rows[:20]]
        + [(adj_service.SETTLE, r.pk) for r in rows[:20] if r.settled_on])
    return render(request, "merchant/marketplace_adjustments.html", _merchant_context(request, store,
        rows=rows, providers=payout_providers(), accounts=adj_service.counter_accounts(store),
        defaults=adj_service.DEFAULT_ACCOUNT, kinds=MarketplaceAdjustment.KINDS, banks=bank_accounts(store),
        open_charges=sum((r.amount for r in open_rows if r.kind == r.CHARGE), 0),
        open_claims=sum((r.amount for r in open_rows if r.kind == r.CLAIM), 0),
        open_count=len(open_rows), today=datetime.date.today(), posted=posted, journal=journal,
        idem_key=idempotency.new_key(), can_edit=_can_edit(request, store)))
