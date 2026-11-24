"""Piutang usaha: how sales invoices reach the ledger.

An order paid through a gateway or a marketplace is booked by its payment, so its invoice is
only a document. Every other invoice (written by hand, sold on credit, B2B) is booked here: the
sale against Piutang usaha when issued, and each payment received against that receivable."""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounting.ledger import (
    ALLOWANCE,
    BAD_DEBT,
    CASH,
    COGS,
    CUSTOMER_CREDIT,
    DISCOUNTS,
    EDC_CLEARING,
    INVENTORY,
    MARKETPLACE,
    PPH_PREPAID,
    RECEIVABLE,
    ROUNDING,
    SALES,
    SHIPPING,
    VAT,
    LedgerError,
    cost_of_goods,
    live_entry,
    post_entry,
    register_backlog,
    reverse_entry,
    reverse_source,
    unposted,
)
from apps.channels.adapters import PLATFORMS
from apps.orders.inventory import return_order_inventory
from apps.orders.models import OrderStatusEvent

from .models import CustomerCredit, Invoice, InvoicePayment

logger = logging.getLogger(__name__)

UNPOSTED_STATUSES = {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}
SETTLED_ON_ISSUE = "Lunas saat invoice diterbitkan"


def booked_by_invoice(invoice):
    """False for invoices whose sale the ledger books elsewhere or not at all: drafts and voids,
    a reseller's downstream sales (not this store's revenue), and orders with a gateway or
    marketplace payment."""
    order = invoice.order
    if invoice.status in UNPOSTED_STATUSES or order.order_type == "reseller_sell_through":
        return False
    return not order.payment_transactions.filter(kind="payment", status="succeeded").exists()


OPENING = "opening"  # Order.order_type of an invoice carried over from the old books


def post_invoice(invoice, *, actor=None):
    if invoice.order.order_type == OPENING:
        # Saldo awal piutang per invoice: sold and taxed in the old books, so no sale, PPN or HPP here,
        # only the receivable against Modal pemilik on the day the books start (tax/opening_invoices.py).
        from apps.accounting import opening
        from apps.accounting.ledger import EQUITY
        start = opening.existing(invoice.store)
        return post_entry(invoice.store, date=start.date if start else invoice.issued_on, actor=actor,
            source=("invoice", invoice.pk), memo=f"Saldo awal piutang {invoice.number} · {invoice.buyer_name}",
            amounts={RECEIVABLE: invoice.total, EQUITY: -invoice.total})
    cost = cost_of_goods(invoice.order)
    # Sales is the residual, so the entry balances whatever the invoice was edited to.
    sales = invoice.total - invoice.shipping - invoice.tax + invoice.discount - invoice.rounding
    from apps.accounting.ledger import DELIVERED
    stock_account = DELIVERED if invoice.from_deliveries else INVENTORY  # goods that left on a surat jalan
    return post_entry(invoice.store, date=invoice.issued_on, actor=actor, cost_center=invoice.cost_center,
        source=("invoice", invoice.pk), memo=f"Invoice {invoice.number} · {invoice.buyer_name}",
        amounts={RECEIVABLE: invoice.total, DISCOUNTS: invoice.discount, SALES: -sales,
            ROUNDING: -invoice.rounding, SHIPPING: -invoice.shipping, VAT: -invoice.tax, COGS: cost,
            stock_account: -cost})


def post_invoice_payment(payment, *, strict=False, actor=None):
    invoice = payment.invoice
    if payment.method == InvoicePayment.METHOD_WRITEOFF:
        from .writeoff import split
        provision, expense = split(invoice.store, payment.amount)
        return post_entry(invoice.store, date=payment.paid_on, strict=strict, actor=actor,
            cost_center=invoice.cost_center, source=("invoice_payment", payment.pk),
            memo=f"Hapus piutang invoice {invoice.number} · {payment.note}",
            amounts={ALLOWANCE: provision, BAD_DEBT: expense, RECEIVABLE: -payment.amount})
    if payment.method == InvoicePayment.METHOD_CREDIT:
        # Settled from the buyer's advance, not new money. PPN already charged on a taxed uang muka was
        # in PPN keluaran since the advance came in: it comes off now, so the sale's PPN counts once.
        return post_entry(invoice.store, date=payment.paid_on, strict=strict, actor=actor,
            cost_center=invoice.cost_center, source=("invoice_payment", payment.pk),
            memo=f"Pembayaran invoice {invoice.number} · {payment.get_method_display()}",
            amounts={CUSTOMER_CREDIT: payment.amount - payment.vat_settled, VAT: payment.vat_settled,
                RECEIVABLE: -payment.amount})
    elif payment.method == InvoicePayment.METHOD_MARKETPLACE:
        received = MARKETPLACE
    elif payment.method in InvoicePayment.CLEARING_METHODS:
        received = EDC_CLEARING  # card or QRIS at the register: with the acquirer until it settles
    elif payment.bank_account_id:
        received = payment.bank_account.account.code
    else:
        received = CASH
    return post_entry(invoice.store, date=payment.paid_on, strict=strict, actor=actor,
        cost_center=invoice.cost_center, source=("invoice_payment", payment.pk),
        memo=f"Pembayaran invoice {invoice.number} · {payment.get_method_display()}",
        amounts={received: payment.amount - payment.withheld, PPH_PREPAID: payment.withheld,
            RECEIVABLE: -payment.amount})


def status_for(invoice, today=None):
    """Paid once payments and credit notes cover the total; overdue once an unpaid balance is past
    its due date (part paid or not: the balance is late either way); part paid once money has come
    in; otherwise open. (A credit note alone lowers the balance but is not a payment.)"""
    paid = sum(payment.amount for payment in invoice.payments.all())
    if paid + invoice.credited >= invoice.total:
        return Invoice.STATUS_PAID
    if invoice.due_date and invoice.due_date < (today or timezone.localdate()):
        return Invoice.STATUS_OVERDUE
    return Invoice.STATUS_PARTIAL if paid > 0 else Invoice.STATUS_ISSUED


def mark_overdue(store=None, today=None):
    """Move open invoices past their due date to overdue, and an overdue one whose due date was
    moved later (or removed) back to open or part paid. Run by `run_books` and the invoice list.
    Returns how many invoices became overdue."""
    today = today or timezone.localdate()
    late = Invoice.objects.filter(status__in=[Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL],
        due_date__lt=today)
    back = Invoice.objects.filter(status=Invoice.STATUS_OVERDUE).filter(
        Q(due_date__isnull=True) | Q(due_date__gte=today))
    if store is not None:
        late, back = late.filter(store=store), back.filter(store=store)
    count = late.update(status=Invoice.STATUS_OVERDUE)
    for invoice in back.select_related("order").prefetch_related("payments", "credit_notes"):
        refresh_status(invoice)
    return count


def refresh_status(invoice):
    """Bring an invoice's status in line with its payments and credit notes."""
    if invoice.status in UNPOSTED_STATUSES:
        return
    status = status_for(invoice)
    if status != invoice.status:
        invoice.status = status
        invoice.save(update_fields=["status"])
        order = invoice.order
        if status == Invoice.STATUS_PAID and order.status == "pending":
            order.status = "paid"
            order.save(update_fields=["status"])
            OrderStatusEvent.objects.create(order=order, status="paid",
                note=f"Invoice {invoice.number} lunas")


@transaction.atomic
def sync_invoice(invoice, *, actor=None):
    """Bring the ledger in line with an invoice. Safe to call any number of times."""
    if not booked_by_invoice(invoice):
        return None
    entry = post_invoice(invoice, actor=actor)
    if (invoice.status == Invoice.STATUS_PAID and invoice.total > 0
            and not invoice.payments.exists()):
        # Issued as already paid (cash sale, marketplace sale typed in by hand): record the
        # payment the status claims, so the receivable closes and the invoice shows no balance.
        method = (InvoicePayment.METHOD_MARKETPLACE if invoice.order.channel in PLATFORMS
            else InvoicePayment.METHOD_OTHER)
        InvoicePayment.objects.create(invoice=invoice, paid_on=invoice.issued_on,
            amount=invoice.total, method=method, note=SETTLED_ON_ISSUE, recorded_by=actor)
    for payment in invoice.payments.all():
        post_invoice_payment(payment, actor=actor)
    return entry


def sync_invoice_safely(invoice, *, actor=None):
    """For callers that must not fail because of the books (imports, order pages): a posting
    that fails is logged and picked up by the backlog."""
    try:
        return sync_invoice(invoice, actor=actor)
    except LedgerError:
        logger.exception("ledger posting failed for invoice %s", invoice.pk)
        return None


@transaction.atomic
def repost_invoice(invoice, *, actor=None):
    """After an invoice was edited: cancel the entry it had and book the new amounts."""
    entry = live_entry(invoice.store, "invoice", invoice.pk)
    if entry:
        reverse_entry(entry, actor=actor, memo=f"Koreksi invoice {invoice.number}")
    refresh_status(invoice)
    return sync_invoice(invoice, actor=actor)


@transaction.atomic
def void_invoice(invoice, *, actor=None):
    """Cancel an invoice: its sale and the payments recorded on it leave the books by
    reversal, never by deletion."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.STATUS_VOID:
        return invoice
    if invoice.credit_notes.exclude(status="void").exists():
        raise LedgerError("Invoice ini punya nota kredit. Batalkan nota kreditnya lebih dulu.")
    memo = f"Pembatalan invoice {invoice.number}"
    if invoice.from_deliveries:
        # The goods left on a surat jalan and stay with the buyer: the deliveries can be invoiced again.
        from .sales_orders import release_invoice
        release_invoice(invoice)
    else:
        # The goods did not leave after all: return what the sale took off the shelf.
        return_order_inventory(invoice.order, actor=actor, reason=memo)
    reverse_source(invoice.store, "invoice", invoice.pk, actor=actor, memo=memo)
    for payment in invoice.payments.all():
        reverse_source(invoice.store, "invoice_payment", payment.pk, actor=actor, memo=memo)
        # Credit that had paid the invoice goes back to the buyer.
        CustomerCredit.objects.filter(payment=payment).update(voided=True)
    invoice.status = Invoice.STATUS_VOID
    invoice.save(update_fields=["status"])
    return invoice


@register_backlog
def post_unposted_invoices():
    posted = 0
    invoices = Invoice.objects.exclude(status__in=UNPOSTED_STATUSES)
    for invoice in unposted(invoices, "invoice"):
        try:
            posted += bool(sync_invoice(invoice))
        except LedgerError:
            logger.exception("ledger posting failed for invoice %s", invoice.pk)
    return posted


OPEN_STATUSES = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]


def paid_elsewhere(store=None):
    """Open invoices whose order was paid through a gateway or marketplace (the payment books the
    sale), so nothing is owed on them: the invoice status simply never caught up."""
    invoices = Invoice.objects.filter(status__in=OPEN_STATUSES, payments__isnull=True,
        order__payment_transactions__kind="payment", order__payment_transactions__status="succeeded",
        order__payment_transactions__reconciled=True).distinct()
    return invoices.filter(store=store) if store else invoices


@register_backlog
def settle_invoices_paid_elsewhere(limit=500):
    """Mark those invoices paid. Returns how many were settled."""
    fixed = 0
    for invoice in paid_elsewhere().select_related("order")[:limit]:
        invoice.status = Invoice.STATUS_PAID
        invoice.save(update_fields=["status"])
        fixed += 1
    return fixed


@register_backlog
def issue_missing_invoices(limit=200):
    """Invoice the paid orders that have none (a marketplace import whose invoice step failed, a
    gateway order paid before invoicing was on). Each is dated by its payment."""
    from .services import issue_pending_invoices
    return issue_pending_invoices(limit=limit)
