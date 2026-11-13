"""Tidying imported sales data, without touching anything the books depend on.

``empty_batches``: an upload that created no order (a wrong file, or the same file again) leaves
an ImportBatch row behind; they only clutter the import history, so they can go.
``duplicate_imports``: the same marketplace order twice (same channel and marketplace order id)
would double the sale. Importing twice is meant to be a no-op, so there should be none; if any are
found they are reported for review, never deleted automatically.
``look_alikes``: orders with the same buyer, total and day. Often a real repeat sale, so they are
only listed."""
from django.db.models import Count

from apps.orders.models import Order

from .models import ChannelOrder, ImportBatch


def empty_batches(store):
    return ImportBatch.objects.filter(store=store, order_count=0).exclude(
        pk__in=ChannelOrder.objects.exclude(batch__isnull=True).values("batch"))


def duplicate_imports(store):
    rows = (Order.objects.filter(store=store).exclude(external_order_id="")
        .values("channel", "external_order_id").annotate(n=Count("id")).filter(n__gt=1))
    return [{"channel": r["channel"], "external_order_id": r["external_order_id"], "count": r["n"],
        "orders": list(Order.objects.filter(store=store, channel=r["channel"],
            external_order_id=r["external_order_id"]).order_by("created_at"))} for r in rows]


def look_alikes(store):
    rows = (Order.objects.filter(store=store).values("customer_name", "total", "created_at__date")
        .annotate(n=Count("id")).filter(n__gt=1))
    return [{"name": r["customer_name"], "total": r["total"], "day": r["created_at__date"],
        "count": r["n"]} for r in rows]


def clean(store, *, apply=False):
    """Report, and with ``apply`` remove the empty import batches. Returns the report."""
    batches = empty_batches(store)
    report = {"empty_batches": batches.count(), "duplicates": duplicate_imports(store),
        "look_alikes": look_alikes(store), "removed": 0}
    if apply:
        report["removed"], _ = batches.delete()
    return report
