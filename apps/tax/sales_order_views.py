"""Pesanan penjualan and surat jalan pages. Logic lives in tax/sales_orders.py."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from apps.accounting.ledger import LedgerError, entries_for
from apps.orders.stock import on_shelf
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail
from apps.shops.permissions import roles

from . import sales_orders
from .models import Delivery, Quote, SalesOrder
from .sales_views import _customer, _doc_form_context, _product_lines
from .services import InvoiceError

LOGIN = "/login/"
VIEW_ROLES = roles("sales.view") | {"viewer"}
EDIT_ROLES = roles("sales.edit")
DELIVER_ROLES = roles("stock.fulfil") | roles("sales.edit")

TABS = [("open", "Terbuka", [SalesOrder.STATUS_OPEN, SalesOrder.STATUS_PARTIAL]),
    ("fulfilled", "Terkirim", [SalesOrder.STATUS_FULFILLED]),
    ("done", "Selesai / batal", [SalesOrder.STATUS_CLOSED, SalesOrder.STATUS_CANCELLED])]


def _amount(text):
    from apps.accounting.bank import parse_amount
    value = parse_amount(str(text or "").strip() or "0")
    if value is None:
        raise InvalidOperation(text)
    return value


def _can(request, store, allowed):
    return store.owner_id == request.user.id or store.staff_members.filter(user=request.user, role__in=allowed).exists()


@login_required(login_url=LOGIN)
def merchant_sales_orders(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    tab = request.GET.get("tab") or "open"
    statuses = {key: status for key, _label, status in TABS}.get(tab, TABS[0][2])
    orders = SalesOrder.objects.filter(store=store, status__in=statuses).select_related("customer").prefetch_related("lines")
    query = request.GET.get("q", "").strip()
    if query:
        from django.db.models import Q
        orders = orders.filter(Q(number__icontains=query) | Q(buyer_name__icontains=query) | Q(company_name__icontains=query))
    counts = {key: SalesOrder.objects.filter(store=store, status__in=status).count() for key, _l, status in TABS}
    backorder = sales_orders.backorder(store)
    return render(request, "merchant/sales_orders.html", _merchant_context(request, store,
        orders=orders.order_by("-issued_on", "-number")[:300], tabs=[(k, l, counts[k]) for k, l, _s in TABS], tab=tab,
        query=query, backorder_units=sum(line.remaining for line in backorder), backorder_lines=backorder.count(),
        can_edit=_can(request, store, EDIT_ROLES)))


@login_required(login_url=LOGIN)
def merchant_sales_order_new(request):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    prefill = _customer(store, request.GET.get("customer"))
    if request.method == "POST":
        try:
            customer = _customer(store, request.POST.get("customer_id"))
            order = sales_orders.create_sales_order(store, lines=_product_lines(request, store),
                buyer_name=request.POST.get("buyer_name") or (customer.name if customer else ""), customer=customer,
                issued_on=parse_date(request.POST.get("issued_on", "") or "") or None,
                expected_on=parse_date(request.POST.get("expected_on", "") or "") or None,
                company_name=request.POST.get("company_name", ""), buyer_npwp=request.POST.get("buyer_npwp", ""),
                phone=request.POST.get("phone", ""), address=request.POST.get("address", ""),
                notes=request.POST.get("notes", ""), discount=_amount(request.POST.get("discount")),
                shipping=_amount(request.POST.get("shipping")), actor=request.user)
        except (sales_orders.SalesOrderError, InvoiceError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        else:
            audit_log(store, request.user, "sales_order.created", order, number=order.number, total=order.total)
            messages.success(request, f"Pesanan penjualan {order.number} dibuat.")
            return redirect("merchant-sales-order-detail", order.pk)
    from collections import defaultdict
    return render(request, "merchant/sales_order_form.html", _doc_form_context(request, store,
        prefill=prefill or defaultdict(str)))


@login_required(login_url=LOGIN)
@require_POST
def merchant_sales_order_from_quote(request, quote_id):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    quote = get_object_or_404(Quote, pk=quote_id, store=store)
    try:
        order = sales_orders.from_quote(quote, actor=request.user)
    except (sales_orders.SalesOrderError, InvoiceError) as error:
        messages.error(request, str(error))
        return redirect("merchant-quote-detail", quote.pk)
    audit_log(store, request.user, "sales_order.created", order, number=order.number, quote=quote.number)
    messages.success(request, f"Pesanan penjualan {order.number} dibuat dari penawaran {quote.number}.")
    return redirect("merchant-sales-order-detail", order.pk)


@login_required(login_url=LOGIN)
def merchant_sales_order_detail(request, order_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    order = get_object_or_404(SalesOrder.objects.select_related("customer", "quote"), pk=order_id, store=store)
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "deliver":
                _require_roles(request, store, DELIVER_ROLES)
                quantities = {}
                for line in order.lines.all():
                    raw = request.POST.get(f"qty_{line.pk}", "").strip()
                    quantities[line.pk] = int(Decimal(raw)) if raw else 0
                delivery = sales_orders.create_delivery(order, quantities=quantities,
                    delivered_on=parse_date(request.POST.get("delivered_on", "") or "") or None,
                    courier=request.POST.get("courier", ""), tracking_number=request.POST.get("tracking_number", ""),
                    notes=request.POST.get("notes", ""), actor=request.user)
                audit_log(store, request.user, "delivery.created", delivery, number=delivery.number, order=order.number)
                messages.success(request, f"Surat jalan {delivery.number} dibuat; stok keluar.")
                return redirect("merchant-delivery-detail", delivery.pk)
            if action == "invoice":
                _require_roles(request, store, EDIT_ROLES)
                chosen = Delivery.objects.filter(pk__in=request.POST.getlist("delivery"), sales_order=order)
                due_days = request.POST.get("due_days", "").strip()
                due = (timezone.localdate() + timezone.timedelta(days=int(due_days))) if due_days.isdigit() else None
                invoice = sales_orders.invoice_deliveries(order, list(chosen), on_credit=request.POST.get("paid") != "1",
                    due_date=due, actor=request.user)
                audit_log(store, request.user, "invoice.created", invoice, number=invoice.number, order=order.number)
                messages.success(request, f"Invoice {invoice.number} dibuat dari surat jalan.")
                return redirect("merchant-invoice-detail", invoice_id=invoice.pk)
            if action == "close":
                _require_roles(request, store, EDIT_ROLES)
                sales_orders.close_order(order, actor=request.user)
                audit_log(store, request.user, "sales_order.closed", order, number=order.number)
                messages.success(request, "Pesanan ditutup; sisa backorder dihapus.")
            else:
                raise Http404
        except (sales_orders.SalesOrderError, InvoiceError, LedgerError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-sales-order-detail", order.pk)
    lines = []
    for line in order.lines.select_related("product"):
        lines.append({"line": line, "shelf": on_shelf(line.product), "remaining": line.remaining,
            "to_invoice": line.delivered_quantity - line.invoiced_quantity})
    deliveries = order.deliveries.select_related("invoice").prefetch_related("lines__product").order_by("delivered_on", "number")
    return render(request, "merchant/sales_order_detail.html", _merchant_context(request, store,
        order=order, rows=lines, deliveries=deliveries,
        uninvoiced=[d for d in deliveries if d.status == Delivery.STATUS_SHIPPED and d.invoice_id is None],
        invoices=order.invoices.order_by("issued_on"), audit=audit_trail(store, order),
        can_edit=_can(request, store, EDIT_ROLES), can_deliver=_can(request, store, DELIVER_ROLES),
        open_for_delivery=order.status in {SalesOrder.STATUS_OPEN, SalesOrder.STATUS_PARTIAL}, today=timezone.localdate()))


@login_required(login_url=LOGIN)
def merchant_deliveries(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES | roles("stock.fulfil"))
    deliveries = Delivery.objects.filter(store=store).select_related("sales_order", "invoice").order_by("-delivered_on", "-number")[:300]
    return render(request, "merchant/deliveries.html", _merchant_context(request, store, deliveries=deliveries,
        waiting=sales_orders.delivered_not_invoiced(store)))


@login_required(login_url=LOGIN)
def merchant_delivery_detail(request, delivery_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES | roles("stock.fulfil"))
    delivery = get_object_or_404(Delivery.objects.select_related("sales_order", "invoice"), pk=delivery_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if request.POST.get("action") != "cancel":
            raise Http404
        try:
            sales_orders.cancel_delivery(delivery, actor=request.user)
        except (sales_orders.SalesOrderError, LedgerError) as error:
            messages.error(request, str(error))
        else:
            audit_log(store, request.user, "delivery.cancelled", delivery, number=delivery.number)
            messages.success(request, "Surat jalan dibatalkan; stok kembali ke rak.")
        return redirect("merchant-delivery-detail", delivery.pk)
    return render(request, "merchant/delivery_detail.html", _merchant_context(request, store, delivery=delivery,
        order=delivery.sales_order, lines=delivery.lines.select_related("product", "order_line"),
        entries=entries_for(store, "delivery", delivery.pk), audit=audit_trail(store, delivery),
        can_edit=_can(request, store, EDIT_ROLES)))


@login_required(login_url=LOGIN)
def merchant_delivery_print(request, delivery_id):
    """Surat jalan to hand the courier: what is in the parcel, to whom, signature boxes, no prices."""
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES | roles("stock.fulfil"))
    delivery = get_object_or_404(Delivery.objects.select_related("sales_order"), pk=delivery_id, store=store)
    return render(request, "tax/delivery_order_note.html", {"delivery": delivery, "order": delivery.sales_order,
        "lines": delivery.lines.select_related("product"), "store": store, "today": timezone.localdate()})
