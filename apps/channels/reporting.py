"""One sales report for every channel, built from the same orders the stock and the ledger use.

Per channel it shows what was sold, what was cancelled or returned, the cost of the goods, the
fees the marketplace kept (when its income report was imported), and the margin that is left.
A mitra's sales are valued at the price the mitra is billed, as in its settlement."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, Sum, Value, When
from django.db.models.functions import Coalesce

from apps.orders.models import Order, OrderItem

from .models import ChannelOrderIncome

CANCELLED = {"cancelled"}
RETURNED = {"returned", "refunded"}
UNPAID = {"pending", "payment_failed"}
MITRA = "reseller_sell_through"
ZERO = Decimal(0)
MONEY = DecimalField(max_digits=18, decimal_places=2)


def _money(value):
    return value or ZERO


def _cost(prefix=""):
    """Unit cost of an item: the cost copied at sale time, else the item's current cost."""
    return Coalesce(F(f"{prefix}unit_cost"), F(f"{prefix}variant__cost_price"),
        F(f"{prefix}product__cost_price"), Value(ZERO), output_field=MONEY)


def _mitra_price(prefix=""):
    return Coalesce(F(f"{prefix}ab_shop_price"), F(f"{prefix}variant__ab_shop_price"),
        F(f"{prefix}product__ab_shop_price"), F(f"{prefix}unit_price"), output_field=MONEY)


def _blank(channel):
    return {"channel": channel, "label": dict(Order.CHANNELS).get(channel, channel), "orders": 0,
        "gross": ZERO, "discount": ZERO, "net_sales": ZERO, "shipping": ZERO, "returns": ZERO,
        "return_count": 0, "cancelled_count": 0, "cancelled_value": ZERO, "cogs": ZERO,
        "fees": ZERO, "fee_orders": 0}


def sales_report(store, start, end, *, channel=None):
    orders = Order.objects.filter(store=store, created_at__date__range=(start, end)).exclude(
        status__in=UNPAID)
    if channel:
        orders = orders.filter(channel=channel)
    rows = defaultdict(lambda: None)

    def row(code):
        if rows[code] is None:
            rows[code] = _blank(code)
        return rows[code]

    selling = orders.exclude(status__in=CANCELLED | RETURNED)
    for r in selling.exclude(order_type=MITRA).values("channel").annotate(
            n=Count("id"), subtotal=Sum("subtotal"), discount=Sum("discount"),
            shipping=Sum("shipping_cost")):
        item = row(r["channel"])
        item["orders"] += r["n"]
        item["gross"] += _money(r["subtotal"])
        item["discount"] += _money(r["discount"])
        item["shipping"] += _money(r["shipping"])
    mitra_orders = selling.filter(order_type=MITRA)
    for r in mitra_orders.values("channel").annotate(n=Count("id"), shipping=Sum("shipping_cost")):
        item = row(r["channel"])
        item["orders"] += r["n"]
        item["shipping"] += _money(r["shipping"])
    for r in OrderItem.objects.filter(order__in=mitra_orders).values("order__channel").annotate(
            value=Sum(ExpressionWrapper(_mitra_price() * F("quantity"), output_field=MONEY))):
        row(r["order__channel"])["gross"] += _money(r["value"])
    for r in orders.filter(status__in=RETURNED).values("channel").annotate(
            n=Count("id"), total=Sum("total")):
        item = row(r["channel"])
        item["return_count"] += r["n"]
        item["returns"] += _money(r["total"])
    for r in orders.filter(status__in=CANCELLED).values("channel").annotate(
            n=Count("id"), total=Sum("total")):
        item = row(r["channel"])
        item["cancelled_count"] += r["n"]
        item["cancelled_value"] += _money(r["total"])
    for r in OrderItem.objects.filter(order__in=selling).values("order__channel").annotate(
            cogs=Sum(ExpressionWrapper(_cost() * F("quantity"), output_field=MONEY))):
        row(r["order__channel"])["cogs"] += _money(r["cogs"])
    fees = sum((F(name) for name in ChannelOrderIncome.FEE_FIELDS[1:]), F("commission"))
    for r in ChannelOrderIncome.objects.filter(channel_order__order__in=selling).values(
            "channel_order__order__channel").annotate(
            fees=Sum(ExpressionWrapper(fees, output_field=MONEY)), n=Count("id")):
        item = row(r["channel_order__order__channel"])
        item["fees"] += _money(r["fees"])
        item["fee_orders"] += r["n"]
    table = sorted(rows.values(), key=lambda item: -item["gross"])
    for item in table:
        item["net_sales"] = item["gross"] - item["discount"]
        item["margin"] = item["net_sales"] - item["cogs"] - item["fees"]
        item["margin_pct"] = (item["margin"] * 100 / item["net_sales"]).quantize(Decimal("0.1")
            ) if item["net_sales"] else ZERO
        seen = item["orders"] + item["return_count"] + item["cancelled_count"]
        item["cancel_pct"] = (Decimal(item["cancelled_count"] + item["return_count"]) * 100
            / seen).quantize(Decimal("0.1")) if seen else ZERO
        item["fees_known"] = item["fee_orders"] == item["orders"] and item["orders"] > 0
    totals = {key: sum((item[key] for item in table), ZERO) for key in (
        "orders", "gross", "discount", "net_sales", "shipping", "returns", "return_count",
        "cancelled_count", "cancelled_value", "cogs", "fees", "margin")}
    totals["margin_pct"] = (totals["margin"] * 100 / totals["net_sales"]).quantize(
        Decimal("0.1")) if totals["net_sales"] else ZERO
    return {"channels": table, "totals": totals, "products": _products(selling),
        "customers": _customers(selling)}


def _products(selling, limit=15):
    items = OrderItem.objects.filter(order__in=selling).annotate(
        sold_value=_revenue(), unit_cost_now=_cost())
    rows = items.values("product_id", "product__name", "product__sku").annotate(
        units=Sum("quantity"), revenue=Sum("sold_value"),
        cogs=Sum(ExpressionWrapper(F("unit_cost_now") * F("quantity"), output_field=MONEY))
    ).order_by("-revenue")[:limit]
    return [{"name": r["product__name"], "sku": r["product__sku"], "quantity": r["units"],
        "revenue": _money(r["revenue"]), "cogs": _money(r["cogs"]),
        "margin": _money(r["revenue"]) - _money(r["cogs"])} for r in rows]


def _revenue():
    """What an item brought in: its line total, or the mitra price for a mitra's sale."""
    return Case(When(order__order_type=MITRA,
        then=ExpressionWrapper(_mitra_price() * F("quantity"), output_field=MONEY)),
        default=F("line_total"), output_field=MONEY)


def _customers(selling, limit=10):
    rows = selling.exclude(customer__isnull=True).values("customer_id", "customer__name").annotate(
        orders=Count("id"), total=Sum("total")).order_by("-total")[:limit]
    return [{"id": r["customer_id"], "name": r["customer__name"], "orders": r["orders"],
        "total": _money(r["total"])} for r in rows]


__all__ = ["sales_report"]
