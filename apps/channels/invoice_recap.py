"""Rekap invoice per channel: what each sales channel (Shopee, Tokopedia, TikTok Shop, Lazada,
toko online, mitra, manual…) invoiced in a period, how much is paid, how much is still owed, and
what credit notes took back. It reads invoices, so it matches the invoice list and the books;
the order-based margin report is ``reporting.sales_report``."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth

from apps.orders.models import Order
from apps.tax.models import CreditNote, Invoice

ZERO = Decimal(0)
OPEN = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]
LIVE = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE, Invoice.STATUS_PAID]


FEE_LABELS = [("commission", "Komisi"), ("service_fee", "Biaya layanan"), ("admin_fee", "Biaya admin"),
    ("shipping_fee", "Selisih ongkir"), ("voucher", "Voucher penjual"), ("affiliate", "Afiliasi"),
    ("other_fee", "Biaya lain")]


def marketplace_summary(store, order):
    """What the marketplace side of an order looks like on its invoice: the shop, the
    marketplace's order number and status, the fees it kept, what it paid out, and when. None for
    an order that did not come from a marketplace."""
    from apps.accounting.models import Payout
    channel_order = getattr(order, "channel_order", None)
    if channel_order is None:
        return None
    income = getattr(channel_order, "income", None)
    summary = {"channel": channel_order.channel, "external_id": channel_order.external_order_id,
        "status_text": order.marketplace_status, "income": income, "fees": [], "payout": None,
        "paid_by_buyer": order.total}
    if income:
        summary["fees"] = [(label, getattr(income, name)) for name, label in FEE_LABELS
            if getattr(income, name)]
        summary["total_fees"] = income.total_fees
        if income.payout_reference:
            summary["payout"] = Payout.objects.filter(store=store,
                reference=income.payout_reference).first()
    return summary


def _blank(code):
    return {"channel": code, "label": dict(Order.CHANNELS).get(code, code), "count": 0,
        "invoiced": ZERO, "tax": ZERO, "credited": ZERO, "credit_count": 0, "net": ZERO,
        "paid": ZERO, "outstanding": ZERO, "overdue": ZERO, "overdue_count": 0, "void_count": 0,
        "fees": ZERO, "released": ZERO, "income_count": 0, "awaiting_count": 0,
        "awaiting_value": ZERO}


def invoice_recap(store, start, end, *, today):
    """``{"channels": [...], "totals": {...}, "months": [...], "month_matrix": {...}}``.
    Invoices are grouped by the channel of their order and the month they were issued."""
    invoices = Invoice.objects.filter(store=store, issued_on__range=(start, end))
    rows = defaultdict(lambda: None)

    def row(code):
        if rows[code] is None:
            rows[code] = _blank(code)
        return rows[code]

    live = invoices.filter(status__in=LIVE)
    for r in live.values("order__channel").annotate(n=Count("id"), total=Sum("total"),
            tax=Sum("tax")):
        item = row(r["order__channel"])
        item["count"], item["invoiced"], item["tax"] = r["n"], r["total"] or ZERO, r["tax"] or ZERO
    for r in invoices.filter(status=Invoice.STATUS_VOID).values("order__channel").annotate(
            n=Count("id")):
        row(r["order__channel"])["void_count"] = r["n"]
    _marketplace_money(live, row)
    notes = CreditNote.objects.filter(invoice__in=live).exclude(status=CreditNote.STATUS_VOID)
    for r in notes.values("invoice__order__channel").annotate(n=Count("id"), total=Sum("total")):
        item = row(r["invoice__order__channel"])
        item["credited"], item["credit_count"] = r["total"] or ZERO, r["n"]
    # What is still owed, and how much of it is late, is per invoice (payments and credit notes).
    for invoice in live.filter(status__in=OPEN).select_related("order").prefetch_related(
            "payments", "credit_notes"):
        due = invoice.amount_due
        if due <= 0:
            continue
        item = row(invoice.order.channel)
        item["outstanding"] += due
        if invoice.due_date and invoice.due_date < today:
            item["overdue"] += due
            item["overdue_count"] += 1
    table = sorted(rows.values(), key=lambda item: -(item["invoiced"] - item["credited"]))
    for item in table:
        item["net"] = item["invoiced"] - item["credited"]
        # Settled is what is no longer owed, whether it came as an invoice payment, a gateway
        # or marketplace payment booked on the order, or a credit note.
        item["paid"] = item["net"] - item["outstanding"]
    keys = ("count", "invoiced", "tax", "credited", "credit_count", "net", "paid", "outstanding",
        "overdue", "overdue_count", "void_count", "fees", "released", "income_count",
        "awaiting_count", "awaiting_value")
    totals = {key: sum((item[key] for item in table), ZERO) for key in keys}
    total_net = totals["net"]
    for item in table:
        item["share"] = (item["net"] * 100 / total_net).quantize(Decimal("0.1")) if total_net else ZERO
    return {"channels": table, "totals": totals, "has_marketplace": any(
        item["income_count"] or item["awaiting_count"] for item in table),
        **_monthly(store, live, notes)}


def _marketplace_money(live, row):
    """For marketplace channels: the fees kept, the money released, and the orders whose income
    report (so fees and payout) has not been imported yet."""
    from django.db.models import F

    from .models import ChannelOrder, ChannelOrderIncome
    fees = sum((F(name) for name in ChannelOrderIncome.FEE_FIELDS[1:]), F("commission"))
    for r in ChannelOrderIncome.objects.filter(channel_order__order__invoice__in=live).values(
            "channel_order__order__channel").annotate(fees=Sum(fees), released=Sum("net_released"),
            n=Count("id")):
        item = row(r["channel_order__order__channel"])
        item["fees"], item["released"], item["income_count"] = (r["fees"] or ZERO,
            r["released"] or ZERO, r["n"])
    for r in ChannelOrder.objects.filter(order__invoice__in=live, income__isnull=True).values(
            "order__channel").annotate(n=Count("id"), total=Sum("order__total")):
        item = row(r["order__channel"])
        item["awaiting_count"], item["awaiting_value"] = r["n"], r["total"] or ZERO


def _monthly(store, live, notes):
    """Net invoiced per month and channel: a month's invoices less the credit notes on them."""
    matrix, months = defaultdict(lambda: defaultdict(lambda: ZERO)), set()
    for r in live.annotate(m=TruncMonth("issued_on")).values("m", "order__channel").annotate(
            total=Sum("total")):
        matrix[r["m"]][r["order__channel"]] += r["total"] or ZERO
        months.add(r["m"])
    for r in notes.annotate(m=TruncMonth("invoice__issued_on")).values(
            "m", "invoice__order__channel").annotate(total=Sum("total")):
        matrix[r["m"]][r["invoice__order__channel"]] -= r["total"] or ZERO
        months.add(r["m"])
    ordered = sorted(months)
    codes = sorted({code for month in ordered for code in matrix[month]},
        key=lambda c: -sum(matrix[m][c] for m in ordered))
    labels = dict(Order.CHANNELS)
    grid = [{"month": m, "cells": [matrix[m][c] for c in codes],
        "total": sum((matrix[m][c] for c in codes), ZERO)} for m in ordered]
    return {"month_channels": [{"code": c, "label": labels.get(c, c)} for c in codes],
        "month_rows": grid,
        "month_totals": [sum((matrix[m][c] for m in ordered), ZERO) for c in codes]}
