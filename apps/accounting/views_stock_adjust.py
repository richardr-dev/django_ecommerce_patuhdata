"""Penyesuaian stok pages: list, new (several lines, one reason), detail with its journal, void."""
import datetime
import uuid as _uuid
from decimal import Decimal, InvalidOperation
from itertools import zip_longest

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalog.models import Product, ProductVariant, Warehouse
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import trail as audit_trail

from . import idempotency, stock_adjustments
from .ledger import entries_for
from .models import ApprovalRequest, StockAdjustment

LOGIN = "/login/"
SCOPE = "stock_adjustment"
MAKE = permissions.roles("stock.adjust")
VIEW_ROLES = MAKE | permissions.roles("stock.adjust_void") | permissions.roles("stock.count") | {"viewer"}


def _catalog(store):
    """Every active stock item with its SKU, current cost and shelf figure, for the picker."""
    items = [{"sku": p.sku, "name": p.name, "cost": str(p.cost_price) if p.cost_price is not None else ""}
        for p in Product.objects.filter(store=store, is_active=True, is_digital=False).exclude(sku="")
            .exclude(variants__is_active=True).distinct().order_by("name")]
    items += [{"sku": v.sku, "name": f"{v.product.name} · {v.name}",
        "cost": str(stock_adjustments.current_cost(v.product, v) or "")}
        for v in ProductVariant.objects.filter(product__store=store, product__is_active=True, is_active=True)
            .exclude(sku="").select_related("product").order_by("product__name", "name")]
    return items


def _rows(post):
    rows = [{"sku": sku.strip(), "qty": qty.strip(), "sign": sign or "-", "cost": cost.strip()}
        for sku, qty, sign, cost in zip_longest(post.getlist("sku[]"), post.getlist("qty[]"),
            post.getlist("sign[]"), post.getlist("cost[]"), fillvalue="")]
    return [row for row in rows if row["sku"]]


def _item(store, sku):
    variant = ProductVariant.objects.filter(product__store=store, sku__iexact=sku, is_active=True).select_related(
        "product").first()
    if variant:
        return variant.product, variant
    product = Product.objects.filter(store=store, sku__iexact=sku, is_active=True).first()
    if not product:
        raise stock_adjustments.AdjustmentError(f"SKU {sku} tidak ada di katalog.")
    return product, None


def _lines(store, reason, rows):
    rule = stock_adjustments.REASONS.get(reason)
    if rule is None:
        raise stock_adjustments.AdjustmentError("Pilih alasan penyesuaian.")
    lines = []
    for row in rows:
        product, variant = _item(store, row["sku"])
        try:
            qty = int(row["qty"] or "0")
            cost = Decimal(row["cost"].replace(".", "").replace(",", "")) if row["cost"] else None
        except (ValueError, InvalidOperation):
            raise stock_adjustments.AdjustmentError(f"Jumlah atau harga pokok {row['sku']} tidak terbaca.") from None
        if qty <= 0:
            raise stock_adjustments.AdjustmentError(f"Jumlah {row['sku']} harus lebih dari nol.")
        sign = {"out": -1, "in": 1}.get(rule.direction, -1 if row["sign"] == "-" else 1)
        lines.append({"product": product, "variant": variant, "quantity": sign * qty,
            "unit_cost": cost if sign > 0 else None})
    return lines


@login_required(login_url=LOGIN)
def merchant_stock_adjustments(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    rows = StockAdjustment.objects.filter(store=store).select_related("warehouse", "created_by")
    reason = request.GET.get("alasan", "")
    if reason in stock_adjustments.REASONS:
        rows = rows.filter(reason=reason)
    today = timezone.localdate()
    by_reason, total = stock_adjustments.summary(store, today.replace(day=1), today)
    return render(request, "merchant/stock_adjustments.html", _merchant_context(request, store,
        rows=rows[:300], reasons=stock_adjustments.REASONS, reason=reason, by_reason=by_reason, month_total=total,
        books_started=stock_adjustments.books_started(store),
        can_make=permissions.can(request.user, store, "stock.adjust")))


@login_required(login_url=LOGIN)
def merchant_stock_adjustment_new(request):
    store = _staff_store(request)
    _require_roles(request, store, MAKE)
    warehouses = list(Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id"))
    today = timezone.localdate()
    post = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        idem = post.get("idem", "")
        earlier = idempotency.claim(store, SCOPE, idem)
        if earlier is not None:
            messages.info(request, "Penyesuaian ini sudah dicatat.")
            return redirect("merchant-stock-adjustment", adjustment_id=earlier) if earlier else redirect(
                "merchant-stock-adjustments")
        try:
            when = datetime.date.fromisoformat(post.get("adjusted_on") or today.isoformat())
            if when > today:
                raise stock_adjustments.AdjustmentError("Tanggal penyesuaian tidak boleh di masa depan.")
            warehouse = next((w for w in warehouses if str(w.pk) == post.get("warehouse")), None)
            if warehouse is None and len(warehouses) == 1:
                warehouse = warehouses[0]
            reason = post.get("reason", "")
            result = stock_adjustments.post_adjustment(store, warehouse=warehouse, adjusted_on=when, reason=reason,
                lines=_lines(store, reason, _rows(post)), notes=post.get("notes", "").strip(), actor=request.user)
        except (stock_adjustments.AdjustmentError, ValueError) as error:
            idempotency.release(store, SCOPE, idem)
            messages.error(request, str(error))
        else:
            if isinstance(result, ApprovalRequest):
                idempotency.finish(store, SCOPE, idem, "")
                messages.info(request, "Nilainya di atas batas persetujuan: penyesuaian diajukan dan dibukukan "
                    "setelah disetujui orang lain.")
                return redirect("merchant-stock-adjustments")
            idempotency.finish(store, SCOPE, idem, result.pk)
            messages.success(request, f"Penyesuaian {result.number} dibukukan: stok dan Persediaan sudah disesuaikan.")
            return redirect("merchant-stock-adjustment", adjustment_id=result.pk)
    return render(request, "merchant/stock_adjustment_new.html", _merchant_context(request, store,
        warehouses=warehouses, reasons=stock_adjustments.REASONS, catalog=_catalog(store),
        rows=_rows(request.POST) if request.method == "POST" else [], post=post,
        chosen_reason=post.get("reason") or request.GET.get("alasan") or "damaged",
        today=today.isoformat(), idem_key=idempotency.new_key()))


@login_required(login_url=LOGIN)
def merchant_stock_adjustment(request, adjustment_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        adjustment = get_object_or_404(StockAdjustment.objects.select_related("warehouse", "created_by", "voided_by"),
            pk=_uuid.UUID(str(adjustment_id)), store=store)
    except ValueError:
        raise Http404 from None
    if request.method == "POST" and request.POST.get("action") == "void":
        _require_roles(request, store, permissions.roles("stock.adjust_void"))
        try:
            stock_adjustments.void_adjustment(adjustment, actor=request.user)
        except stock_adjustments.AdjustmentError as error:
            messages.error(request, f"Gagal: {error}")
        else:
            messages.success(request, "Penyesuaian dibatalkan: stok dikembalikan dan jurnalnya dibalik.")
        return redirect("merchant-stock-adjustment", adjustment_id=adjustment.pk)
    from .ledger import chart_of_accounts
    account = chart_of_accounts(store).get(adjustment.account_code)
    return render(request, "merchant/stock_adjustment_detail.html", _merchant_context(request, store,
        adjustment=adjustment, lines=list(adjustment.lines.select_related("product", "variant")),
        account=account, journal=entries_for(store, [(stock_adjustments.SOURCE, adjustment.pk)]),
        audit=audit_trail(store, adjustment),
        can_void=adjustment.status == StockAdjustment.POSTED and permissions.can(request.user, store,
            "stock.adjust_void")))
