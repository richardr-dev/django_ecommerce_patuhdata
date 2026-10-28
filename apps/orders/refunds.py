from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.integrations.http import ProviderError
from apps.integrations.models import OutboxEvent
from apps.integrations.payments import get_payment_provider
from apps.shops.models import AuditLog

from .models import Order, PaymentTransaction, Refund
from .services import InvalidOrderTransition, transition_order


class RefundError(ValueError):
    pass


@transaction.atomic
def create_refund_request(*, order, amount, reason, actor, note="", method="gateway"):
    order = Order.objects.select_for_update().get(id=order.id)
    payment = PaymentTransaction.objects.select_for_update().filter(order=order, kind="payment",
        status="succeeded", reconciled=True).order_by("created_at").first()
    if not payment:
        raise RefundError("Tidak ada pembayaran terverifikasi yang dapat direfund.")
    amount = Decimal(amount)
    committed = Refund.objects.filter(order=order, status__in={"pending", "succeeded"}).aggregate(
        total=Sum("amount"))["total"] or Decimal(0)
    if amount <= 0 or committed + amount > payment.amount:
        raise RefundError("Total refund tidak boleh melebihi pembayaran terverifikasi.")
    refund = Refund.objects.create(order=order, payment=payment, amount=amount, reason=reason,
        note=note, actor=actor, method=method)
    if order.status not in {"refund_requested", "partially_refunded"}:
        try:
            transition_order(order, "refund_requested", note=f"Refund diajukan: {note}")
        except InvalidOrderTransition as error:
            raise RefundError(str(error)) from error
    AuditLog.objects.create(store=order.store, actor=actor, action="refund.requested",
        object_type="Refund", object_id=str(refund.id), details={"order": str(order.id),
            "amount": str(amount), "reason": reason})
    return refund


def execute_refund(refund):
    try:
        result = get_payment_provider(refund.payment.provider).refund(refund.payment, refund)
    except ProviderError as error:
        refund.status, refund.payload = "failed", {"error": str(error)}
        refund.save(update_fields=["status", "payload", "updated_at"])
        raise
    return apply_refund_result(refund, status=result.status, reference=result.reference,
        payload=result.payload)


def record_manual_refund(refund, *, transfer_reference, actor):
    """Record a refund that finance paid by bank transfer outside the gateway."""
    transfer_reference = transfer_reference.strip()[:120]
    if not transfer_reference:
        raise RefundError("Nomor referensi transfer wajib diisi untuk refund manual.")
    return apply_refund_result(refund, status="succeeded", reference=f"manual-{refund.id}",
        payload={"method": "manual_transfer", "transfer_reference": transfer_reference,
            "recorded_by": actor.get_username()})


@transaction.atomic
def apply_refund_result(refund, *, status, reference, payload):
        refund = Refund.objects.select_for_update().select_related("order", "payment").get(id=refund.id)
        refund.status, refund.provider_reference, refund.payload = status, reference, payload
        refund.save(update_fields=["status", "provider_reference", "payload", "updated_at"])
        PaymentTransaction.objects.update_or_create(provider=refund.payment.provider,
            external_id=reference or str(refund.id), kind="refund", defaults={"order": refund.order,
                "provider_payment_id": refund.payment.provider_payment_id, "status": status,
                "amount": refund.amount, "currency": refund.payment.currency,
                "reconciled": status == "succeeded", "payload": payload})
        if status == "succeeded" and refund.order.status in {
            "refund_requested", "partially_refunded"
        }:
            total = Refund.objects.filter(order=refund.order, status="succeeded").aggregate(
                value=Sum("amount"))["value"] or Decimal(0)
            target = "refunded" if total >= refund.payment.amount else "partially_refunded"
            if target != refund.order.status:
                transition_order(refund.order, target, note=f"Refund {refund.amount} berhasil")
            OutboxEvent.objects.create(store=refund.order.store, topic="order.refunded",
                aggregate_id=str(refund.order_id), payload={"order_id": str(refund.order_id),
                    "refund_id": str(refund.id), "amount": str(refund.amount)})
        AuditLog.objects.create(store=refund.order.store, actor=refund.actor,
            action=f"refund.{status}", object_type="Refund", object_id=str(refund.id),
            details={"provider_reference": reference, "amount": str(refund.amount)})
        return refund
