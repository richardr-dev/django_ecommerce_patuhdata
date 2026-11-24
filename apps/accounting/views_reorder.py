"""Usulan pembelian and Aturan stok minimum. Logic lives in accounting/reordering.py."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.catalog.models import Warehouse
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import idempotency, purchasing, reordering
from .models import ReorderRule, Supplier

LOGIN = "/login/"
ORDER_ROLES = permissions.roles("purchase.order")
VIEW_ROLES = ORDER_ROLES | permissions.roles("stock.receive") | {"viewer"}


def _whole(raw, default=0):
    raw = str(raw or "").strip().replace(".", "").replace(",", "")
    return int(raw) if raw else default


def _money(raw):
    from .bank import parse_amount
    value = parse_amount(str(raw or "").strip() or "0")
    if value is None:
        raise InvalidOperation(raw)
    return value


def _supplier(store, value):
    return Supplier.objects.filter(store=store, pk=value).first() if str(value or "").isdigit() else None


def _warehouse(store, value):
    return Warehouse.objects.filter(store=store, is_active=True, pk=value).first() if str(value or "").isdigit() else None


@login_required(login_url=LOGIN)
def merchant_reorder(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, ORDER_ROLES)
        action = request.POST.get("action")
        if action == "order":
            return _order(request, store)
        if action == "save_rule":
            return _save_rule(request, store)
        if action == "delete_rule":
            rule = ReorderRule.objects.filter(store=store, pk=_whole(request.POST.get("rule"))).first()
            if rule is not None:
                audit_log(store, request.user, "reorder_rule.deleted", rule, item=rule.label)
                rule.delete()
                messages.success(request, "Aturan dihapus.")
            return redirect(f"{request.path}?tab=aturan")
        raise Http404
    tab = "aturan" if request.GET.get("tab") == "aturan" else "usulan"
    suggestions = reordering.suggestions(store)
    rules = list(ReorderRule.objects.filter(store=store).select_related("product", "variant", "warehouse", "supplier"))
    for rule in rules:
        rule.where = reordering.position(rule)
    from apps.tax.sales_views import _goods_items
    warehouses = list(Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id"))
    return render(request, "merchant/reorder.html", _merchant_context(request, store, tab=tab,
        suggestions=suggestions, rules=rules, items=_goods_items(store), warehouses=warehouses,
        suppliers=Supplier.objects.filter(store=store, is_active=True).order_by("name"),
        total=sum((s.value for s in suggestions), Decimal(0)), idem_key=idempotency.new_key(),
        can_order=permissions.can(request.user, store, "purchase.order")))


def _order(request, store):
    key = request.POST.get("idem", "")
    earlier = idempotency.claim(store, "reorder", key)
    if earlier is not None:
        messages.info(request, "Pesanan pembelian dari usulan ini sudah dibuat.")
        return redirect("merchant-purchase-orders")
    rules = {str(r.pk): r for r in ReorderRule.objects.filter(store=store).select_related("product", "variant", "warehouse")}
    try:
        picks = []
        for rule_id in request.POST.getlist("pick"):
            rule = rules.get(rule_id)
            if rule is None:
                continue
            picks.append((rule, _whole(request.POST.get(f"qty_{rule_id}")),
                _supplier(store, request.POST.get(f"supplier_{rule_id}")), _money(request.POST.get(f"cost_{rule_id}"))))
        made = reordering.order(store, picks, order_date=timezone.localdate(), actor=request.user)
    except (reordering.ReorderError, purchasing.PurchaseError, InvalidOperation, ValueError) as error:
        idempotency.release(store, "reorder", key)
        messages.error(request, f"Gagal: {error}")
        return redirect("merchant-reorder")
    idempotency.finish(store, "reorder", key, made[0].pk)
    for po in made:
        audit_log(store, request.user, "purchase_order.created", po, supplier=po.supplier.name, value=po.value,
            source="usulan pembelian")
    messages.success(request, f"{len(made)} pesanan pembelian dibuat: " + ", ".join(po.number for po in made) + ".")
    if len(made) == 1:
        return redirect("merchant-purchase-order", po_id=made[0].pk)
    return redirect("merchant-purchase-orders")


def _save_rule(request, store):
    from apps.tax.invoice_form_views import _item
    from apps.tax.services import InvoiceError
    post = request.POST
    rule = ReorderRule.objects.filter(store=store, pk=_whole(post.get("rule"))).first() if post.get("rule") else None
    try:
        product, variant = _item(store, post.get("item"))
        saved = reordering.save_rule(store, rule=rule, product=product, variant=variant,
            warehouse=_warehouse(store, post.get("warehouse")), minimum=_whole(post.get("minimum")),
            maximum=_whole(post.get("maximum")), multiple=_whole(post.get("multiple"), 1),
            supplier=_supplier(store, post.get("supplier")), lead_days=_whole(post.get("lead_days")))
    except (reordering.ReorderError, InvoiceError, ValueError) as error:
        messages.error(request, f"Gagal: {error}")
    else:
        audit_log(store, request.user, "reorder_rule.saved", saved, item=saved.label, minimum=saved.minimum,
            maximum=saved.maximum)
        messages.success(request, f"Aturan stok minimum {saved.label} disimpan.")
    return redirect(f"{request.path}?tab=aturan")
