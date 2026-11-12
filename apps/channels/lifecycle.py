"""Keeps an imported marketplace order in step with the marketplace.

A sale is imported once, but the marketplace keeps changing it: shipped, completed, cancelled,
returned. Every later import (a file or the API) of the same order lands here. A cancellation or
return takes the sale back everywhere it was recorded: the stock goes back on the shelf, the
journal entries are reversed, and the invoice is voided. Each step is safe to repeat."""
import logging

from django.db import transaction

from apps.accounting.ledger import LedgerError, post_payment, reverse_source
from apps.orders.inventory import return_order_inventory
from apps.orders.models import Order, OrderStatusEvent
from apps.tax.models import CreditNote, Invoice
from apps.tax.posting import void_invoice

from .adapters import (
    PLATFORMS,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_RANK,
    STATE_RETURNED,
    STATE_SHIPPED,
)

logger = logging.getLogger(__name__)

# The Order status each marketplace state maps to.
ORDER_STATUS = {STATE_SHIPPED: "shipped", STATE_COMPLETED: "completed",
    STATE_CANCELLED: "cancelled", STATE_RETURNED: "returned"}
TERMINAL = {"cancelled", "returned", "refunded"}
REVERSING = {STATE_CANCELLED, STATE_RETURNED}


def _note(channel, state, text):
    label = PLATFORMS[channel.platform].label
    names = {STATE_CANCELLED: "dibatalkan", STATE_RETURNED: "dikembalikan",
        STATE_COMPLETED: "selesai", STATE_SHIPPED: "dikirim"}
    return f"{label}: pesanan {names.get(state, state)}" + (f" ({text})" if text else "")


def take_back_sale(order, *, reason, actor=None):
    """Undo everything a sale put on the books: stock, journal and invoice."""
    from apps.tax.credit_notes import CreditNoteError, void_credit_note
    for note in CreditNote.objects.filter(invoice__order=order).exclude(status="void"):
        try:  # the whole sale is undone below, so corrections to it go first
            void_credit_note(note, actor=actor)
        except CreditNoteError:
            logger.exception("credit note %s could not be voided with order %s", note.pk, order.pk)
    return_order_inventory(order, actor=actor, reason=reason)
    for payment in order.payment_transactions.filter(kind="payment", status="succeeded"):
        # Book it first if the worker has not yet, so the reversal leaves a complete trail.
        if not hasattr(payment, "journal_entry"):
            try:
                post_payment(payment)
            except LedgerError:
                logger.exception("ledger posting failed for payment %s", payment.pk)
                continue
        reverse_source(order.store, "payment", payment.pk, actor=actor,
            memo=f"Pembatalan penjualan #{str(order.id)[:8]} · {reason}")
    invoice = Invoice.objects.filter(order=order).exclude(status=Invoice.STATUS_VOID).first()
    if invoice:
        void_invoice(invoice, actor=actor)


@transaction.atomic
def sync_order_state(channel, order, data):
    """Apply the marketplace's current state to an order already imported. Returns True when
    anything changed. An order never moves backwards, and a cancelled or returned one is final."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    state = data.state
    changed = False
    if data.status_text and order.marketplace_status != data.status_text[:60]:
        order.marketplace_status = data.status_text[:60]
        order.save(update_fields=["marketplace_status"])
        changed = True
    if order.status in TERMINAL:
        return changed
    if state in REVERSING:
        take_back_sale(order, reason=_note(channel, state, data.status_text))
        order.status = ORDER_STATUS[state]
    elif state in ORDER_STATUS and STATE_RANK.get(state, 0) > _rank(order.status):
        order.status = ORDER_STATUS[state]
    else:
        return changed
    order.save(update_fields=["status"])
    OrderStatusEvent.objects.create(order=order, status=order.status,
        note=_note(channel, state, data.status_text))
    return True


def _rank(status):
    if status in {"shipped", "delivered"}:
        return STATE_RANK[STATE_SHIPPED]
    if status == "completed":
        return STATE_RANK[STATE_COMPLETED]
    return STATE_RANK["active"]
