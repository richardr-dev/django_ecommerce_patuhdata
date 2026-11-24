"""Saldo awal piutang per invoice: invoices the business issued before its books started here.

A company moving onto Patuhdata usually has customers who still owe on invoices from the old system.
Typing their total into Saldo awal works for the balance sheet, but then nobody can receive a payment
against "INV-0912 from March", age it, remind the buyer or print a statement. An opening invoice
fixes that: it is an ordinary ``Invoice`` (paid, aged, reminded, written off, on the customer
statement like any other) carried by an order of type ``opening``.

What it is not: a sale. The sale and its PPN were booked and reported in the old books, so posting
(``posting.post_invoice``) books only Piutang usaha against Modal pemilik on the day the books start
(the Saldo awal date, else the invoice's own date). It takes no stock, has one line, cannot be
edited, credited, duplicated or returned, and the sales reports, the channel recap, the sales export
and the PPN worksheets leave it out. Voiding it reverses that entry.

The receivable total typed on the Saldo awal form is for what is *not* entered here; the page shows
both so nothing is counted twice."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.shops.models import Customer

from .models import Invoice
from .posting import OPENING
from .sales import InvoiceError, _build_order, _write_items, clean_lines

LINE_TEXT = "Saldo awal piutang"


class OpeningInvoiceError(InvoiceError):
    pass


def opening_invoices(store):
    return (Invoice.objects.filter(store=store, order__order_type=OPENING)
        .select_related("order__customer").prefetch_related("payments", "credit_notes")
        .order_by("issued_on", "number"))


def totals(store):
    """``(count, original total, still owed)`` of the live opening invoices."""
    rows = [invoice for invoice in opening_invoices(store) if invoice.status != Invoice.STATUS_VOID]
    return len(rows), sum((i.total for i in rows), Decimal(0)), sum((i.amount_due for i in rows), Decimal(0))


def books_start(store):
    from apps.accounting import opening
    entry = opening.existing(store)
    return entry.date if entry else None


@transaction.atomic
def create_opening_invoice(store, *, customer, number, issued_on, amount, due_date=None, notes="", actor=None):
    """Carry one unpaid invoice over from the old books. ``amount`` is what the customer still owes
    on it today (a part-paid old invoice comes over with its remainder)."""
    from .posting import status_for
    from .services import issue_invoice_for_order
    if customer is None or customer.store_id != store.id:
        raise OpeningInvoiceError("Pilih pelanggan.")
    number = (number or "").strip()[:40]
    if not number:
        raise OpeningInvoiceError("Isi nomor invoice dari sistem lama.")
    if Invoice.objects.filter(store=store, number=number).exists():
        raise OpeningInvoiceError(f"Nomor invoice {number} sudah dipakai.")
    amount = Decimal(amount or 0)
    if amount <= 0:
        raise OpeningInvoiceError("Sisa tagihan harus lebih dari nol.")
    if issued_on is None:
        raise OpeningInvoiceError("Isi tanggal invoice.")
    start = books_start(store)
    if start and issued_on > start:
        raise OpeningInvoiceError(f"Invoice bertanggal setelah saldo awal ({start:%d/%m/%Y}) adalah penjualan "
            "baru: buat lewat Buat invoice.")
    if due_date and due_date < issued_on:
        raise OpeningInvoiceError("Jatuh tempo tidak boleh sebelum tanggal invoice.")
    lines = clean_lines([{"product": None, "description": LINE_TEXT, "qty": 1, "unit_price": amount}])
    order = _build_order(store, lines=lines, buyer_name=customer.company_name or customer.name,
        issued_on=issued_on, channel="manual", customer=customer, phone=customer.phone or "-",
        address=customer.address or "-", notes=notes or "", external_order_id="", discount=0, shipping=0,
        status="pending", subtotal=amount, tax=0, total=amount, order_type=OPENING)
    # Due date first: the invoice is posted while it is issued, and its status depends on it.
    invoice, _ = issue_invoice_for_order(order, issued_on=issued_on, notes=notes or "", number=number)
    _write_items(invoice, lines)
    invoice.due_date = due_date
    invoice.status = status_for(invoice)
    invoice.save(update_fields=["due_date", "status"])
    from apps.shops import audit
    audit.log(store, actor, "invoice.opening_created", invoice, amount=str(amount))
    return invoice


# ── Paste from a spreadsheet (parser shared with Saldo awal › Utang per tagihan) ──

def parse(text):
    """Rows pasted from Excel or typed: ``pelanggan; nomor; tanggal; jatuh tempo; sisa tagihan``."""
    from apps.accounting.opening_paste import parse as parse_rows
    return parse_rows(text, party="pelanggan", document="invoice")


def find_customer(store, name):
    match = Customer.objects.filter(store=store, name__iexact=name.strip()).first()
    return match or Customer.objects.filter(store=store, company_name__iexact=name.strip()).first()


@transaction.atomic
def import_rows(store, rows, *, actor=None):
    """Create every row or none. A customer that does not exist yet is added by name. Returns the
    invoices; raises ``OpeningInvoiceError`` naming the first bad line."""
    bad = [row for row in rows if row.error]
    if bad:
        raise OpeningInvoiceError(f"Baris {bad[0].line}: {bad[0].error}.")
    if not rows:
        raise OpeningInvoiceError("Tidak ada baris untuk diimpor.")
    from apps.accounting.opening_paste import duplicates
    twice = duplicates(rows)
    if twice:
        raise OpeningInvoiceError(f"Nomor invoice dobel di daftar: {', '.join(twice)}.")
    created = []
    for row in rows:
        customer = find_customer(store, row.party) or Customer.objects.create(store=store, name=row.party[:120])
        try:
            created.append(create_opening_invoice(store, customer=customer, number=row.number,
                issued_on=row.issued_on, due_date=row.due_date, amount=row.amount, actor=actor))
        except InvoiceError as error:
            raise OpeningInvoiceError(f"Baris {row.line}: {error}") from error
    return created


def receivable_in_opening_entry(store):
    """Piutang usaha typed as a total on the Saldo awal form (not itemised)."""
    from apps.accounting import opening
    from apps.accounting.ledger import RECEIVABLE
    entry = opening.existing(store)
    if not entry:
        return Decimal(0)
    return entry.lines.filter(account__code=RECEIVABLE).aggregate(total=Sum("debit", default=0))["total"]
