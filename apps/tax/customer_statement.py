"""Laporan rekening pelanggan: everything between the company and one customer over a period.

The open-item surat tagihan (``receivables.statement``) lists what is unpaid today. This is the
activity statement accountants send at month end: the balance at the start, every invoice (debit),
payment, credit note and write-off (credit) in date order with a running balance, and the balance
at the end. Read from the documents themselves, so the closing balance equals what the open
invoices still owe (pinned by the tests). Void invoices and their payments are left out: voiding
reverses them in the books too. Customer credit (uang muka not yet used) is a separate liability
and is shown beside the balance, not netted into it."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from .models import CreditNote, CustomerCredit, Invoice, InvoicePayment

ZERO = Decimal(0)


@dataclass
class Statement:
    customer: object
    start: date
    end: date
    opening: Decimal = ZERO
    lines: list = field(default_factory=list)
    invoiced: Decimal = ZERO
    received: Decimal = ZERO
    credit_available: Decimal = ZERO

    @property
    def closing(self):
        return self.lines[-1]["balance"] if self.lines else self.opening


def _events(customer):
    """``(date, order, kind, number, description, debit, credit, link id)`` for the customer's
    documents, oldest first. ``order`` keeps an invoice before a payment on the same day."""
    invoices = Invoice.objects.filter(order__customer=customer).exclude(
        status__in=[Invoice.STATUS_VOID, Invoice.STATUS_DRAFT])
    for invoice in invoices.select_related("order").prefetch_related("payments", "credit_notes"):
        yield (invoice.issued_on, 0, "invoice", invoice.number,
            f"Invoice{' · jatuh tempo ' + invoice.due_date.strftime('%d/%m/%Y') if invoice.due_date else ''}",
            invoice.total, ZERO, invoice.pk)
        if invoice.status == Invoice.STATUS_PAID:
            # Paid through a gateway or a marketplace: settled without an InvoicePayment row.
            elsewhere = invoice.total - invoice.amount_paid - invoice.credited
            if elsewhere > 0:
                yield (invoice.issued_on, 1, "payment", invoice.number,
                    f"Lunas lewat {invoice.order.get_channel_display()}", ZERO, elsewhere, invoice.pk)
    payments = InvoicePayment.objects.filter(invoice__in=invoices).select_related("invoice")
    for payment in payments:
        writeoff = payment.method == InvoicePayment.METHOD_WRITEOFF
        label = "Penghapusan piutang" if writeoff else f"Pembayaran · {payment.get_method_display()}"
        if payment.withheld:
            label += " (termasuk PPh dipotong)"
        yield (payment.paid_on, 1, "writeoff" if writeoff else "payment", payment.invoice.number, label,
            ZERO, payment.amount, payment.invoice_id)
    notes = CreditNote.objects.filter(invoice__in=invoices).exclude(status=CreditNote.STATUS_VOID)
    for note in notes.select_related("invoice"):
        if note.applied:
            yield (note.issued_on, 2, "credit_note", note.number,
                f"Nota kredit untuk {note.invoice.number}", ZERO, note.applied, note.invoice_id)


def build(store, customer, start, end):
    statement = Statement(customer=customer, start=start, end=end)
    events = sorted((event for event in _events(customer) if event[0] <= end),
        key=lambda event: (event[0], event[1], str(event[3])))
    balance = ZERO
    for when, _order, kind, number, text, debit, credit, link in events:
        balance += debit - credit
        if when < start:
            statement.opening = balance
            continue
        statement.invoiced += debit
        statement.received += credit
        statement.lines.append({"date": when, "kind": kind, "number": number, "text": text,
            "debit": debit, "credit": credit, "balance": balance, "invoice_id": link})
    # Uang muka and credit not yet used, as it stood at the end of the period (same rows as Kredit pelanggan).
    statement.credit_available = CustomerCredit.objects.filter(store=store, customer=customer, voided=False,
        date__lte=end).aggregate(total=Sum("amount", default=0))["total"]
    return statement
