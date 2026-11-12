"""One filter for the sales invoice list and everything exported from it.

The list page, its CSV, Excel and PDF exports all read the same query parameters through
``apply_filters``, so what you see is what you export. Parameters (all optional):

  q          invoice no., order ref, buyer name, phone, or product name / SKU
  channel    one sales channel (shopee, tokopedia, web…)
  inv        invoice tab: awaiting, overdue, paid, draft, void, none
  status     order status
  range      today | 7d | 30d | month | lastmonth | year
  date_from, date_to   a custom range (wins over ``range``)
  month      YYYY-MM (older links)
  min_total, max_total  order total in rupiah

The date is the invoice date, or the order date while the order has no invoice."""
import datetime
import re
from decimal import Decimal, InvalidOperation

from django.db.models import Exists, OuterRef, Q
from django.utils.dateparse import parse_date

from apps.orders.models import Order, OrderItem
from apps.tax.models import Invoice

PRESETS = [("today", "Hari ini"), ("7d", "7 hari"), ("30d", "30 hari"), ("month", "Bulan ini"),
    ("lastmonth", "Bulan lalu"), ("year", "Tahun ini")]
MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
TABS = [
    ("", "Semua", None),
    ("draft", "Draft", Q(invoice__status=Invoice.STATUS_DRAFT)),
    ("awaiting", "Menunggu pembayaran", Q(invoice__status__in=[Invoice.STATUS_ISSUED,
        Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE])),
    ("overdue", "Jatuh tempo", Q(invoice__status=Invoice.STATUS_OVERDUE)),
    ("paid", "Lunas", Q(invoice__status=Invoice.STATUS_PAID)),
    ("void", "Dibatalkan", Q(invoice__status=Invoice.STATUS_VOID)),
    ("none", "Belum diterbitkan", Q(invoice__isnull=True)),
]
# Every query parameter this filter owns, so a link or a form can carry or clear them.
KEYS = ("q", "channel", "inv", "status", "range", "date_from", "date_to", "month", "min_total",
    "max_total")


def _day(value):
    try:
        return parse_date(value or "")
    except ValueError:
        return None


def _short(day):
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def date_range(params, today):
    """``(start, end, label)``; ``start`` and ``end`` are dates or None."""
    start, end = _day(params.get("date_from")), _day(params.get("date_to"))
    if start or end:
        label = (f"{_short(start)} – {_short(end)}" if start and end else
            f"Sejak {_short(start)}" if start else f"Sampai {_short(end)}")
        return start, end, label
    month = _day((params.get("month") or "") + "-01") if params.get("month") else None
    if month:
        last = (month.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
        return month, last, f"{MONTHS[month.month - 1]} {month.year}"
    preset = params.get("range") or ""
    first = today.replace(day=1)
    if preset == "today":
        return today, today, "Hari ini"
    if preset == "7d":
        return today - datetime.timedelta(days=6), today, "7 hari terakhir"
    if preset == "30d":
        return today - datetime.timedelta(days=29), today, "30 hari terakhir"
    if preset == "month":
        return first, today, "Bulan ini"
    if preset == "lastmonth":
        end = first - datetime.timedelta(days=1)
        return end.replace(day=1), end, "Bulan lalu"
    if preset == "year":
        return today.replace(month=1, day=1), today, "Tahun ini"
    return None, None, ""


def _amount(value):
    try:
        number = Decimal(str(value or "").replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None
    return number if number >= 0 else None


def _phone_tail(text):
    digits = re.sub(r"\D", "", text)
    if len(digits) < 5:
        return ""
    for prefix in ("62", "0"):
        if digits.startswith(prefix):
            return digits[len(prefix):]
    return digits


def search(qs, text):
    """Invoice number, order ref, buyer, phone, or a product bought on the order."""
    q = Q(customer_name__icontains=text) | Q(invoice_number__icontains=text) \
        | Q(external_order_id__icontains=text) | Q(invoice__number__icontains=text) \
        | Q(invoice__buyer_name__icontains=text)
    tail = _phone_tail(text)
    if tail:
        q |= Q(phone__icontains=tail) | Q(customer__phone__icontains=tail)
    bought = OrderItem.objects.filter(order=OuterRef("pk")).filter(
        Q(name__icontains=text) | Q(sku__icontains=text))
    return qs.filter(q | Q(Exists(bought)))


def apply_filters(qs, params, today=None):
    """Narrow an Order queryset. Returns ``(queryset, active)``; ``active`` lists what is applied
    as ``{"label", "keys"}`` so the page can show removable chips."""
    today = today or datetime.date.today()
    active = []
    text = (params.get("q") or "").strip()
    if text:
        qs = search(qs, text)
        active.append({"label": f"Cari “{text}”", "keys": ["q"]})
    channel = params.get("channel") or ""
    if channel:
        qs = qs.filter(channel=channel)
        active.append({"label": dict(Order.CHANNELS).get(channel, channel), "keys": ["channel"]})
    status = params.get("status") or ""
    if status:
        qs = qs.filter(status=status)
        label = dict(Order._meta.get_field("status").choices).get(status, status)
        active.append({"label": f"Pesanan: {label}", "keys": ["status"]})
    start, end, label = date_range(params, today)
    if start or end:
        by_invoice, by_order = Q(invoice__isnull=False), Q(invoice__isnull=True)
        if start:
            by_invoice &= Q(invoice__issued_on__gte=start)
            by_order &= Q(created_at__date__gte=start)
        if end:
            by_invoice &= Q(invoice__issued_on__lte=end)
            by_order &= Q(created_at__date__lte=end)
        qs = qs.filter(by_invoice | by_order)
        active.append({"label": label, "keys": ["range", "date_from", "date_to", "month"]})
    low, high = _amount(params.get("min_total")), _amount(params.get("max_total"))
    if low is not None and params.get("min_total"):
        qs = qs.filter(total__gte=low)
    if high is not None and params.get("max_total"):
        qs = qs.filter(total__lte=high)
    if (low is not None and params.get("min_total")) or (high is not None and params.get("max_total")):
        parts = ([f"≥ Rp{low:,.0f}"] if low is not None and params.get("min_total") else []) + \
            ([f"≤ Rp{high:,.0f}"] if high is not None and params.get("max_total") else [])
        active.append({"label": "Total " + " ".join(parts).replace(",", "."),
            "keys": ["min_total", "max_total"]})
    return qs, active


def apply_tab(qs, code):
    condition = {key: cond for key, _, cond in TABS}.get(code)
    return qs if condition is None else qs.filter(condition)
