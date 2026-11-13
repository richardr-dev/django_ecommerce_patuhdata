"""Sales analytics: how much, through which channel, to whom, and from where.

Everything is read from the orders the store made or imported in a period, with the same filter
as the invoice list (period, channels). A sale is an order that was not cancelled, failed or
refunded, paid or not yet: an invoice on credit is still a sale. The previous period of the same
length is computed for comparison. The places come from the order, else from the marketplace buyer
(Tokopedia hides street addresses but gives city and province)."""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Sum

from apps.orders.models import Order, OrderItem, ReturnRequest

from .invoice_filters import apply_filters, date_range

NOT_A_SALE = {"cancelled", "payment_failed", "refunded"}
WEEKDAYS = ("Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min")
UNKNOWN = "Tidak diketahui"


def _key(customer_id, phone, name):
    """Who a buyer is: the CRM customer, else the phone number, else the name."""
    if customer_id:
        return f"c:{customer_id}"
    if phone and phone.strip() not in {"", "-"}:
        return f"p:{''.join(ch for ch in phone if ch.isdigit())[-9:]}"
    return f"n:{(name or '').strip().lower()}"


def _delta(now, before):
    if not before:
        return None
    return float((Decimal(now) - Decimal(before)) / Decimal(before) * 100)


def _bars(rows, value, limit=None):
    """Add ``pct`` (share of the biggest) to each row for drawing."""
    rows = rows[:limit] if limit else rows
    top = max((value(r) for r in rows), default=0) or 1
    for row in rows:
        row["pct"] = round(float(value(row)) / float(top) * 100, 1)
    return rows


def _sales(store, params, today):
    base = Order.objects.filter(store=store)
    scoped, active = apply_filters(base, params, today)
    return scoped.exclude(status__in=NOT_A_SALE), scoped, active


def analyze(store, params, today=None):
    today = today or datetime.date.today()
    params = params.copy()
    if not any(params.get(k) for k in ("range", "date_from", "date_to", "month")):
        params["range"] = "30d"
    start, end, label = date_range(params, today)
    end = end or today
    start = start or (end - datetime.timedelta(days=29))
    sales, scoped, active = _sales(store, params, today)
    span = (end - start).days + 1

    previous = params.copy()
    for key in ("range", "month"):
        previous.pop(key, None)
    previous["date_from"] = (start - datetime.timedelta(days=span)).isoformat()
    previous["date_to"] = (start - datetime.timedelta(days=1)).isoformat()
    before, _, _ = _sales(store, previous, today)

    now_totals = sales.aggregate(revenue=Sum("total"), orders=Count("id"))
    before_totals = before.aggregate(revenue=Sum("total"), orders=Count("id"))
    revenue, orders = now_totals["revenue"] or Decimal(0), now_totals["orders"] or 0

    rows = list(sales.values_list("pk", "customer_id", "phone", "customer_name", "total", "created_at",
        "channel", "province", "city", "channel_order__buyer__province", "channel_order__buyer__city"))
    channel_names = dict(Order.CHANNELS)
    daily, weekday = defaultdict(lambda: [Decimal(0), 0]), [[Decimal(0), 0] for _ in range(7)]
    by_channel = defaultdict(lambda: {"revenue": Decimal(0), "orders": 0, "buyers": set()})
    by_province = defaultdict(lambda: {"revenue": Decimal(0), "orders": 0, "buyers": set()})
    by_city = defaultdict(lambda: {"revenue": Decimal(0), "orders": 0, "buyers": set(), "province": ""})
    buyers = defaultdict(lambda: {"name": "", "orders": 0, "revenue": Decimal(0), "channels": set()})
    for pk, customer_id, phone, name, total, created, channel, province, city, b_province, b_city in rows:
        local = created.astimezone().date() if hasattr(created, "astimezone") else created
        key = _key(customer_id, phone, name)
        daily[local][0] += total
        daily[local][1] += 1
        weekday[local.weekday()][0] += total
        weekday[local.weekday()][1] += 1
        for table, name_key in ((by_channel, channel),):
            table[name_key]["revenue"] += total
            table[name_key]["orders"] += 1
            table[name_key]["buyers"].add(key)
        place_province = (province or b_province or "").strip() or UNKNOWN
        place_city = (city or b_city or "").strip() or UNKNOWN
        by_province[place_province]["revenue"] += total
        by_province[place_province]["orders"] += 1
        by_province[place_province]["buyers"].add(key)
        by_city[place_city]["revenue"] += total
        by_city[place_city]["orders"] += 1
        by_city[place_city]["buyers"].add(key)
        by_city[place_city]["province"] = by_city[place_city]["province"] or (
            place_province if place_province != UNKNOWN else "")
        buyer = buyers[key]
        buyer["name"] = buyer["name"] or name
        buyer["orders"] += 1
        buyer["revenue"] += total
        buyer["channels"].add(channel)

    # new or returning: had they bought before this period?
    earlier = {_key(c, p, n) for c, p, n in Order.objects.filter(store=store,
        created_at__date__lt=start).exclude(status__in=NOT_A_SALE).values_list(
        "customer_id", "phone", "customer_name")}
    returning = sum(1 for key in buyers if key in earlier)

    # the trend: a bar per day, or per week for long periods
    weekly = span > 92
    series, cursor = [], start
    if weekly:
        cursor = start - datetime.timedelta(days=start.weekday())
    while cursor <= end:
        step = 7 if weekly else 1
        days = [cursor + datetime.timedelta(days=i) for i in range(step)]
        series.append({"label": cursor, "revenue": sum((daily[d][0] for d in days if start <= d <= end),
            Decimal(0)), "orders": sum(daily[d][1] for d in days if start <= d <= end)})
        cursor += datetime.timedelta(days=step)
    peak = max(series, key=lambda s: s["revenue"], default=None)
    _bars(series, lambda s: s["revenue"])

    channels = sorted(({"code": code, "label": channel_names.get(code, code), "revenue": v["revenue"],
        "orders": v["orders"], "buyers": len(v["buyers"])} for code, v in by_channel.items()),
        key=lambda r: -r["revenue"])
    for row in channels:
        row["share"] = round(float(row["revenue"]) / float(revenue) * 100, 1) if revenue else 0
        row["avg"] = row["revenue"] / row["orders"] if row["orders"] else 0
    _bars(channels, lambda r: r["revenue"])

    def places(table, limit):
        out = sorted(({"name": n, "revenue": v["revenue"], "orders": v["orders"], "buyers": len(v["buyers"]),
            "province": v.get("province", "")} for n, v in table.items()), key=lambda r: -r["revenue"])
        known = [r for r in out if r["name"] != UNKNOWN]
        unknown = [r for r in out if r["name"] == UNKNOWN]
        return _bars(known[:limit], lambda r: r["revenue"]), (unknown[0] if unknown else None)

    provinces, unknown_province = places(by_province, 8)
    cities, unknown_city = places(by_city, 10)

    items = (OrderItem.objects.filter(order__in=sales).values("name", "sku").annotate(
        units=Sum("quantity"), revenue=Sum("line_total"), orders=Count("order", distinct=True))
        .order_by("-revenue")[:8])
    products = _bars([dict(row) for row in items], lambda r: r["revenue"])

    top_customers = sorted(({"name": b["name"] or "—", "orders": b["orders"], "revenue": b["revenue"],
        "channels": [channel_names.get(c, c) for c in sorted(b["channels"])]} for b in buyers.values()),
        key=lambda r: -r["revenue"])[:8]
    _bars(top_customers, lambda r: r["revenue"])

    weekdays = _bars([{"label": WEEKDAYS[i], "revenue": weekday[i][0], "orders": weekday[i][1]}
        for i in range(7)], lambda r: r["orders"])

    cancelled = scoped.filter(status__in={"cancelled", "payment_failed"}).count()
    returns = ReturnRequest.objects.filter(order__in=scoped).count()
    return {
        "start": start, "end": end, "label": label, "span": span, "weekly": weekly, "active": active,
        "revenue": revenue, "orders": orders, "avg": revenue / orders if orders else Decimal(0),
        "buyers": len(buyers), "returning": returning, "new": len(buyers) - returning,
        "returning_pct": round(returning / len(buyers) * 100) if buyers else 0,
        "revenue_delta": _delta(revenue, before_totals["revenue"] or 0),
        "orders_delta": _delta(orders, before_totals["orders"] or 0),
        "previous_revenue": before_totals["revenue"] or Decimal(0),
        "series": series, "peak": peak, "channels": channels, "provinces": provinces,
        "unknown_province": unknown_province, "cities": cities, "unknown_city": unknown_city,
        "products": products, "customers": top_customers, "weekdays": weekdays,
        "cancelled": cancelled, "cancel_pct": round(cancelled / (orders + cancelled) * 100, 1)
            if orders + cancelled else 0,
        "returns": returns, "return_pct": round(returns / orders * 100, 1) if orders else 0,
    }
