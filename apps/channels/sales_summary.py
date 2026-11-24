"""Ringkasan penjualan: what was sold in a period across every channel, from the orders.

Revenue here is order value (what buyers were charged) less the refunds paid in the period, so a refunded order
does not count as a sale. The accounting figure (net of PPN, returns and fees) is the Laba rugi; the page links
there. Bar widths are returned as strings with a dot, because the Indonesian locale would print a float with a
decimal comma and the bar would lose its width."""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from apps.orders.models import Order, OrderItem, Refund

REVENUE_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed", "shipped", "delivered", "completed",
    "refund_requested", "partially_refunded", "refunded"}
ZERO = Decimal(0)


def _pct(value, top):
    return f"{(value / top * 100) if top else 0:.1f}"


def _date(text, default):
    try:
        return date.fromisoformat(text) if text else default
    except ValueError:
        return default


def period(params):
    today = timezone.localdate()
    start = _date(params.get("date_from", ""), today - timedelta(days=29))
    end = _date(params.get("date_to", ""), today)
    if start > end:
        start, end = end, start
    return start, end


def summary(store, params, *, detail=True):
    start, end = period(params)
    channel = params.get("channel", "")
    base = Order.objects.filter(store=store, status__in=REVENUE_STATUSES).exclude(order_type="opening")
    if channel:
        base = base.filter(channel=channel)
    orders = base.filter(created_at__date__gte=start, created_at__date__lte=end)
    totals = orders.aggregate(gross=Sum("total", default=ZERO), count=Count("id"),
        customers=Count("phone", distinct=True), discount=Sum("discount", default=ZERO),
        shipping=Sum("shipping_cost", default=ZERO))
    refunds = Refund.objects.filter(order__store=store, status="succeeded",
        created_at__date__gte=start, created_at__date__lte=end)
    if channel:
        refunds = refunds.filter(order__channel=channel)
    refunded = refunds.aggregate(t=Sum("amount", default=ZERO))["t"]
    gross, count = totals["gross"], totals["count"]
    labels = dict(Order.CHANNELS)
    channels = list(orders.values("channel").annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("-revenue"))
    for row in channels:
        row["label"] = labels.get(row["channel"], row["channel"])
        row["pct"] = _pct(row["revenue"], gross)
    result = {"date_from": start, "date_to": end, "channel_filter": channel, "gross": gross, "refunds": refunded,
        "revenue": gross - refunded, "order_count": count, "aov": gross / count if count else ZERO,
        "unique_customers": totals["customers"], "total_discount": totals["discount"],
        "total_shipping": totals["shipping"], "channel_rows": channels,
        "channel_choices": [(c, labels.get(c, c)) for c in Order.objects.filter(store=store)
            .values_list("channel", flat=True).distinct().order_by("channel")]}
    if not detail:
        return result
    daily = list(orders.annotate(day=TruncDate("created_at")).values("day")
        .annotate(revenue=Sum("total"), orders=Count("id")).order_by("day"))
    peak = max((r["revenue"] for r in daily), default=ZERO)
    for row in daily:
        row["pct"] = _pct(row["revenue"], peak)
    monthly = list(base.annotate(month=TruncMonth("created_at")).values("month")
        .annotate(revenue=Sum("total"), orders=Count("id")).order_by("-month")[:12])
    top = max((r["revenue"] for r in monthly), default=ZERO)
    for row in monthly:
        row["pct"] = _pct(row["revenue"], top)
    result.update(daily=daily, monthly=monthly,
        top_products=list(OrderItem.objects.filter(order__in=orders).values("name", "sku")
            .annotate(units=Sum("quantity"), revenue=Sum("line_total")).order_by("-revenue")[:10]),
        recent_orders=list(Order.objects.filter(store=store, created_at__date__gte=start,
            created_at__date__lte=end).exclude(order_type="opening").order_by("-created_at")[:15]))
    return result
