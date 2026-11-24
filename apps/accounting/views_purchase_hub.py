"""Ringkasan pembelian: the purchasing flow on one page (usulan → pesanan → penerimaan → tagihan → bayar),
with what waits at each step and a shortcut to every purchasing document. It reads, never writes."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions

from . import payables, purchasing, reordering
from .models import GoodsReceipt, PurchaseOrder

LOGIN = "/login/"
VIEW_ROLES = (permissions.roles("books.view") | permissions.roles("purchase.order")
    | permissions.roles("stock.receive") | {"viewer"})
SHOWN = 5


def _step(number, title, help_text, url, count, waiting, *, late=0, late_text=""):
    return {"number": number, "title": title, "help": help_text, "url": url, "count": count,
        "waiting": waiting, "late": late, "late_text": late_text}


@login_required(login_url=LOGIN)
def merchant_purchasing(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    can = {key.replace(".", "_"): permissions.can(request.user, store, key)
        for key in ("books.view", "books.edit", "purchase.order", "purchase.cash", "stock.receive")}

    to_order = reordering.suggestions(store)
    open_pos = list(PurchaseOrder.objects.filter(store=store, status__in=[PurchaseOrder.OPEN, PurchaseOrder.PARTIAL])
        .select_related("supplier").prefetch_related("lines").order_by("expected_on", "order_date"))
    late_pos = [po for po in open_pos if po.expected_on and po.expected_on < today]
    unbilled = list(GoodsReceipt.objects.filter(store=store, status=GoodsReceipt.STATUS_OPEN)
        .select_related("supplier").prefetch_related("lines").order_by("received_on"))
    bills = payables.open_bills(store) if can["books_view"] else []
    overdue = [b for b in bills if b.due_date and b.due_date < today]
    due_soon = [b for b in bills if b.due_date and today <= b.due_date <= today + timedelta(days=7)]

    steps = [
        _step(1, "Usulan pembelian", "Barang di bawah stok minimum, siap dijadikan pesanan.",
            reverse("merchant-reorder"), len(to_order), "barang perlu dipesan"),
        _step(2, "Pesanan pembelian", "Yang sudah dipesan ke supplier dan belum datang semua.",
            reverse("merchant-purchase-orders"), len(open_pos), "pesanan terbuka",
            late=len(late_pos), late_text="terlambat"),
        _step(3, "Penerimaan barang", "Barang yang datang masuk gudang; tagihan menyusul.",
            reverse("merchant-goods-receipts"), len(unbilled), "penerimaan belum ditagih"),
        _step(4, "Tagihan supplier", "Faktur supplier, dibuat dari penerimaan atau langsung.",
            reverse("merchant-bills"), len(bills), "tagihan belum lunas",
            late=len(overdue), late_text="lewat jatuh tempo"),
        _step(5, "Bayar supplier", "Bayar satu atau beberapa tagihan sekaligus per supplier.",
            reverse("merchant-payables"), len(due_soon), "jatuh tempo 7 hari ke depan",
            late=len(overdue), late_text="sudah lewat"),
    ]
    if not can["books_view"]:
        steps = steps[:3]

    totals = {
        "on_order": purchasing.waiting_value(store),
        "unbilled": sum((r.value for r in unbilled), Decimal(0)),
        "owed": sum((b.amount_due for b in bills), Decimal(0)),
        "overdue": sum((b.amount_due for b in overdue), Decimal(0)),
    }
    return render(request, "merchant/purchasing.html", _merchant_context(request, store,
        steps=steps, totals=totals, can=can, today=today,
        to_order=to_order[:SHOWN], late_pos=late_pos[:SHOWN], unbilled=unbilled[:SHOWN],
        overdue=overdue[:SHOWN], due_soon=due_soon[:SHOWN]))
