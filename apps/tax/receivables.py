"""Piutang usaha per pelanggan: who owes what, and for how long.

Built from open sales invoices, so it matches the Piutang usaha account. Age is counted from the
due date (the issue date when an invoice has none)."""
from collections import OrderedDict
from decimal import Decimal

from .models import Invoice

BUCKETS = [("current", "Belum jatuh tempo"), ("d30", "1–30 hari"), ("d60", "31–60 hari"),
    ("d90", "61–90 hari"), ("over", "Lebih dari 90 hari")]
OPEN = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]


def bucket_for(invoice, as_of):
    late = (as_of - (invoice.due_date or invoice.issued_on)).days
    return ("current" if late <= 0 else "d30" if late <= 30 else "d60" if late <= 60
        else "d90" if late <= 90 else "over")


def _party(invoice):
    order = invoice.order
    if order.customer_id:
        return f"c{order.customer_id}", order.customer.name, order.customer_id
    return f"n{invoice.buyer_name.lower()}", invoice.buyer_name, None


def open_invoices(store, *, customer_id=None):
    invoices = Invoice.objects.filter(store=store, status__in=OPEN).select_related(
        "order__customer").prefetch_related("payments", "credit_notes")  # amount_due reads both
    if customer_id:
        invoices = invoices.filter(order__customer_id=customer_id)
    return [invoice for invoice in invoices.order_by("issued_on") if invoice.amount_due > 0]


def aging(store, as_of):
    """Open balance per customer, split into age buckets, largest debtor first."""
    parties = OrderedDict()
    for invoice in open_invoices(store):
        key, name, customer_id = _party(invoice)
        row = parties.setdefault(key, {"name": name, "customer_id": customer_id, "total": Decimal(0),
            "oldest": invoice.due_date or invoice.issued_on,
            **{bucket: Decimal(0) for bucket, _ in BUCKETS}})
        due = invoice.amount_due
        row[bucket_for(invoice, as_of)] += due
        row["total"] += due
    rows = sorted(parties.values(), key=lambda row: -row["total"])
    totals = {bucket: sum((row[bucket] for row in rows), Decimal(0)) for bucket, _ in BUCKETS}
    totals["total"] = sum((row["total"] for row in rows), Decimal(0))
    return rows, totals


def statement(store, customer_id, as_of):
    """A customer's open invoices with what was paid on each: the statement to send them."""
    lines = []
    for invoice in open_invoices(store, customer_id=customer_id):
        lines.append({"invoice": invoice, "paid": invoice.amount_paid, "due": invoice.amount_due,
            "late_days": max(0, (as_of - (invoice.due_date or invoice.issued_on)).days),
            "bucket": bucket_for(invoice, as_of)})
    return lines, sum((line["due"] for line in lines), Decimal(0))
