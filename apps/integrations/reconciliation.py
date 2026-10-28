from decimal import Decimal

from django.db import transaction

from apps.orders.models import PaymentTransaction
from apps.orders.services import transition_order

from .models import OutboxEvent


@transaction.atomic
def confirm_payment(order, *, provider, external_id, amount, currency, payload,
        provider_payment_id="", note):
    """Record a gateway-confirmed payment on a locked order.

    Returns "" when the order was (or already is) paid, otherwise the mismatch reason. A wrong
    amount/currency is recorded as a failed transaction; a correct payment that arrives after the
    order left ``pending`` (for example after expiry) is kept as an unreconciled success so finance
    can refund it.
    """
    amount = Decimal(str(amount))
    mismatch = "" if amount == order.total and currency == "IDR" else "Amount or currency mismatch"
    state_mismatch = order.status not in {"pending", "paid"}
    if state_mismatch and not mismatch:
        mismatch = f"Order status {order.status} cannot accept payment"
    PaymentTransaction.objects.update_or_create(provider=provider, external_id=external_id,
        kind="payment", defaults={"order": order,
            "status": "failed" if mismatch and not state_mismatch else "succeeded",
            "provider_payment_id": provider_payment_id, "amount": amount, "currency": currency or "IDR",
            "reconciled": not mismatch, "mismatch_reason": mismatch, "payload": payload})
    if mismatch:
        return mismatch
    if order.status == "pending":
        transition_order(order, "paid", note=note)
        OutboxEvent.objects.create(store=order.store, topic="payment.received",
            aggregate_id=str(order.id), payload={"order_id": str(order.id),
                "phone": order.phone, "total": str(order.total)})
    return ""
