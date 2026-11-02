"""What a shopper sees of their orders, marketplace style: status tabs, an estimated arrival
date and a tracker. Shoppers who have not signed in see the orders this browser placed."""
import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Order

BUYER_TABS = [
    ("unpaid", "Belum Bayar", {"pending"}),
    ("packing", "Dikemas", {"paid", "confirmed", "allocated", "picking", "packed"}),
    ("shipping", "Dikirim", {"shipped"}),
    ("done", "Selesai", {"delivered", "completed"}),
    ("cancelled", "Dibatalkan", {"payment_failed", "cancelled", "refund_requested",
        "partially_refunded", "refunded", "returned"}),
]
ACTIVE_STATUSES = BUYER_TABS[0][2] | BUYER_TABS[1][2] | BUYER_TABS[2][2]
# The seller usually hands a paid order to the courier within this many days.
PACKING_DAYS = 1


def buyer_orders(request):
    """Orders of the signed-in shopper, plus any this browser placed as a guest."""
    session_ids = request.session.get("customer_order_ids", [])
    mine = Q(id__in=session_ids)
    if request.user.is_authenticated:
        mine |= Q(customer_user=request.user)
    return Order.objects.filter(mine, store=request.store)


def active_order_count(request):
    if not getattr(request, "store", None):
        return 0
    if not request.user.is_authenticated and not request.session.get("customer_order_ids"):
        return 0
    return buyer_orders(request).filter(status__in=ACTIVE_STATUSES).count()


def _add_days(start, days, business):
    day = start
    while days > 0:
        day += timedelta(days=1)
        if not business or day.weekday() < 5:
            days -= 1
    return day


def _status_time(order, status):
    times = getattr(order, "_status_times", None)
    if times is None:
        times = {event.status: event.created_at for event in order.status_events.all()}
        order._status_times = times
    return times.get(status)


def estimated_arrival(order):
    """(earliest, latest) arrival dates from the courier's quoted delivery time, e.g.
    "2–4 hari kerja", counted from shipping (or from today plus packing time before that).
    None when there is no quote or the order is not on its way."""
    if order.status not in BUYER_TABS[1][2] | {"shipped"}:
        return None
    numbers = [int(value) for value in re.findall(r"\d+", order.shipping_eta or "")]
    if not numbers:
        return None
    business = "kerja" in order.shipping_eta.lower() or "business" in order.shipping_eta.lower()
    shipped = _status_time(order, "shipped") if order.status == "shipped" else None
    start = timezone.localdate(shipped) if shipped else _add_days(timezone.localdate(),
        PACKING_DAYS, business)
    return (_add_days(start, min(numbers), business), _add_days(start, max(numbers), business))


def tracker_steps(order):
    """The progress bar on the order page: each step with its time once reached."""
    steps = [("Dipesan", order.created_at), ("Dibayar", _status_time(order, "paid")),
        ("Dikemas", _status_time(order, "packed")), ("Dikirim", _status_time(order, "shipped")),
        ("Diterima", _status_time(order, "delivered") or _status_time(order, "completed"))]
    reached = {"pending": 1, "payment_failed": 1, "paid": 2, "confirmed": 2, "allocated": 2,
        "picking": 2, "packed": 3, "shipped": 4, "delivered": 5, "completed": 5}.get(order.status, 0)
    return [{"label": label, "at": at, "done": index < reached}
        for index, (label, at) in enumerate(steps)]
