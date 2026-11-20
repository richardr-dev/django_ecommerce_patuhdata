"""Writing off an invoice the buyer will not pay (penghapusan piutang).

The unpaid balance leaves Piutang usaha. It is taken from the receivable provision (Cadangan kerugian
piutang, 1255) first, as far as that has been set aside; whatever the provision does not cover goes to
Beban piutang tak tertagih (6970). The write-off is an ordinary payment of method "writeoff", so the
invoice status, the aging and voiding all keep working: voiding the payment puts the receivable back."""
import datetime
from decimal import Decimal

from django.db import transaction

from apps.accounting.ledger import ALLOWANCE, LedgerError, account_balances
from apps.shops import audit

from .models import Invoice, InvoicePayment
from .posting import post_invoice, post_invoice_payment, refresh_status
from .services import InvoiceError


def provision_available(store):
    """What is set aside in 1255 and not yet used (it is a credit balance)."""
    balances = {a.code: a.balance for a in account_balances(store)}
    return max(-balances.get(ALLOWANCE, Decimal(0)), Decimal(0))


def split(store, amount):
    """How a write-off of `amount` divides: (from the provision, to the expense)."""
    from_provision = min(provision_available(store), amount)
    return from_provision, amount - from_provision


@transaction.atomic
def write_off_invoice(invoice, *, date=None, reason, actor=None):
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("Tulis alasan penghapusan piutang.")
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status in {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}:
        raise InvoiceError("Invoice ini tidak bisa dihapus piutangnya.")
    due = invoice.amount_due
    if due <= 0:
        raise InvoiceError("Invoice ini sudah lunas, tidak ada piutang yang dihapus.")
    date = date or datetime.date.today()
    if date < invoice.issued_on:
        raise InvoiceError("Tanggal penghapusan tidak boleh sebelum tanggal invoice.")
    payment = InvoicePayment.objects.create(invoice=invoice, paid_on=date, amount=due,
        method=InvoicePayment.METHOD_WRITEOFF, note=reason[:200], recorded_by=actor)
    try:
        post_invoice(invoice, actor=actor)
        post_invoice_payment(payment, strict=True, actor=actor)
    except LedgerError as error:
        raise InvoiceError(str(error)) from error
    refresh_status(invoice)
    audit.log(invoice.store, actor, "invoice.written_off", invoice, amount=due, reason=reason[:200])
    return payment
