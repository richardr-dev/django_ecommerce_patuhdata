"""Lembar kerja SPT Masa PPN: PPN keluaran against PPN masukan, with the overpayment of earlier
months carried forward (kompensasi).

The amounts are the ledger's own: PPN keluaran is what Utang PPN moved by in the month (invoices,
less credit notes), PPN masukan is what PPN masukan moved by (supplier bills and expenses of a PKP).
So the worksheet always agrees with the books. A month that ends below zero is a lebih bayar: it is
carried to the next month, which owes only what is left after using it."""
import datetime
from decimal import Decimal

from django.db.models import Sum

from apps.accounting.ledger import INPUT_VAT, VAT
from apps.accounting.models import Bill, Expense, JournalLine

from .models import CreditNote, CustomerReceipt, Invoice, InvoicePayment

ZERO = Decimal(0)
LOOKBACK_MONTHS = 24


def month_bounds(year, month):
    first = datetime.date(year, month, 1)
    return first, (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)


def _previous(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _ledger(store, code, first, last):
    row = JournalLine.objects.filter(entry__store=store, account__code=code, entry__date__gte=first,
        entry__date__lte=last).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (row["d"] or ZERO), (row["c"] or ZERO)


def keluaran(store, year, month):
    first, last = month_bounds(year, month)
    debit, credit = _ledger(store, VAT, first, last)
    return credit - debit


def masukan(store, year, month):
    first, last = month_bounds(year, month)
    debit, credit = _ledger(store, INPUT_VAT, first, last)
    return debit - credit


def _first_active_month(store, year, month):
    """The earliest month (within two years) with any PPN, where the carry-forward starts."""
    earliest = JournalLine.objects.filter(entry__store=store, account__code__in=[VAT, INPUT_VAT]
        ).order_by("entry__date").values_list("entry__date", flat=True).first()
    floor_year, floor_month = year - (LOOKBACK_MONTHS // 12), month
    start = datetime.date(floor_year, floor_month, 1)
    if earliest is None:
        return year, month
    return max((earliest.year, earliest.month), (start.year, start.month))


def worksheet(store, year, month):
    """Everything the monthly PPN return is prepared from, for one month."""
    y, m = _first_active_month(store, year, month)
    carry = ZERO
    while (y, m) < (year, month):
        net = keluaran(store, y, m) - masukan(store, y, m)
        position = net - carry
        carry = -position if position < 0 else ZERO
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    first, last = month_bounds(year, month)
    out, inn = keluaran(store, year, month), masukan(store, year, month)
    net = out - inn
    position = net - carry
    sales = Invoice.objects.filter(store=store, issued_on__gte=first, issued_on__lte=last, tax__gt=0
        ).exclude(status__in=[Invoice.STATUS_VOID, Invoice.STATUS_DRAFT]).select_related("order").order_by(
        "issued_on", "number")
    credits = CreditNote.objects.filter(store=store, issued_on__gte=first, issued_on__lte=last, tax__gt=0,
        status=CreditNote.STATUS_ISSUED).select_related("invoice")
    purchases = Bill.objects.filter(store=store, bill_date__gte=first, bill_date__lte=last, tax__gt=0
        ).exclude(status__in=[Bill.STATUS_VOID, Bill.STATUS_DRAFT]).select_related("supplier")
    expenses = Expense.objects.filter(store=store, date__gte=first, date__lte=last, input_vat__gt=0,
        approval_pending=False)
    # Uang muka kena PPN: its faktur counts in the month the money came in; the PPN is taken off again
    # in the month the advance pays an invoice (that invoice's faktur lists the uang muka as already taxed).
    advances = CustomerReceipt.objects.filter(store=store, date__gte=first, date__lte=last, deposit_vat__gt=0,
        voided=False).order_by("date", "number")
    advances_used = InvoicePayment.objects.filter(invoice__store=store, paid_on__gte=first, paid_on__lte=last,
        vat_settled__gt=0).exclude(invoice__status=Invoice.STATUS_VOID).select_related("invoice").order_by("paid_on")
    dpp_out = sales.aggregate(t=Sum("tax_base"))["t"] or ZERO
    dpp_returns = credits.aggregate(t=Sum("subtotal"))["t"] or ZERO
    due_next = (last + datetime.timedelta(days=1)).replace(day=1)
    deadline = (due_next.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    # Walk-in register sales (no buyer NPWP) are reported to DJP as one digunggung total for the
    # month, not per invoice: list only the rest, and count only the rest as missing a buyer id.
    from .faktur import is_pos_digunggung
    all_sales = list(sales)
    digunggung_sales = [invoice for invoice in all_sales if is_pos_digunggung(invoice)]
    itemised = [invoice for invoice in all_sales if invoice not in digunggung_sales]
    digunggung = {"count": len(digunggung_sales), "dpp": sum((i.tax_base for i in digunggung_sales), ZERO),
        "tax": sum((i.tax for i in digunggung_sales), ZERO)}
    return {"year": year, "month": month, "start": first, "end": last, "out": out, "in": inn, "net": net,
        "carry_in": carry, "to_pay": position if position > 0 else ZERO,
        "overpaid": -position if position < 0 else ZERO, "dpp_out": dpp_out, "dpp_returns": dpp_returns,
        "sales": itemised, "digunggung": digunggung, "credits": credits, "purchases": purchases,
        "expenses": expenses, "deadline": deadline, "advances": advances, "advances_used": advances_used,
        "missing_npwp": sum(1 for invoice in itemised if not invoice.buyer_npwp)}
