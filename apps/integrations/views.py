import hashlib
import hmac
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from apps.orders.models import Order, PaymentEvent, PaymentTransaction
from apps.orders.services import InvalidOrderTransition, transition_order

from .models import OutboxEvent


def demo_payment(request, order_id):
    if not request.store: raise Http404
    order = get_object_or_404(Order, id=order_id, store=request.store)
    if request.method == "POST":
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order.id)
            if order.status == "pending":
                PaymentTransaction.objects.update_or_create(provider="demo", external_id=order.payment_reference,
                    kind="payment", defaults={"order": order, "status": "succeeded", "amount": order.total,
                        "currency": "IDR", "reconciled": True})
                transition_order(order, "paid", note=f"Pembayaran {order.payment_method.upper()} diterima")
                OutboxEvent.objects.create(store=order.store, topic="payment.received", aggregate_id=str(order.id),
                    payload={"order_id": str(order.id), "phone": order.phone, "total": str(order.total)})
        return redirect("order-detail", order_id=order.id)
    return render(request, "storefront/demo_payment.html", {"order": order})


@csrf_exempt
def xendit_webhook(request):
    if request.method != "POST": return HttpResponse(status=405)
    expected = settings.XENDIT_WEBHOOK_VERIFICATION_TOKEN
    received = request.headers.get("x-callback-token", "")
    if not expected or not hmac.compare_digest(received, expected):
        return JsonResponse({"error": "Invalid webhook token"}, status=401)
    try: payload = json.loads(request.body)
    except json.JSONDecodeError: return JsonResponse({"error": "Invalid JSON"}, status=400)
    event_type, data = payload.get("event", ""), payload.get("data", {})
    reference = data.get("reference_id", "")
    if event_type not in {"payment_session.completed", "payment_session.expired"} or not reference:
        return JsonResponse({"received": True})
    with transaction.atomic():
        try: order = Order.objects.select_for_update().get(id=reference, payment_provider="xendit")
        except (Order.DoesNotExist, ValueError): return JsonResponse({"error": "Order not found"}, status=404)
        event_id = hashlib.sha256(request.body).hexdigest()
        _, created = PaymentEvent.objects.get_or_create(store=order.store, provider="xendit",
            external_id=event_id, defaults={"event_type": event_type, "payload": payload})
        if not created: return JsonResponse({"received": True, "duplicate": True})
        if data.get("payment_session_id") and data["payment_session_id"] != order.payment_reference:
            return JsonResponse({"error": "Payment reference mismatch"}, status=400)
        if event_type == "payment_session.completed":
            amount = Decimal(str(data.get("amount", -1)))
            currency = data.get("currency", "")
            mismatch = "" if amount == order.total and currency == "IDR" else "Amount or currency mismatch"
            PaymentTransaction.objects.update_or_create(provider="xendit",
                external_id=data.get("payment_session_id") or order.payment_reference, kind="payment",
                defaults={"order": order, "status": "failed" if mismatch else "succeeded", "amount": amount,
                    "currency": currency or "IDR", "reconciled": not mismatch,
                    "mismatch_reason": mismatch, "payload": data})
            if mismatch:
                return JsonResponse({"error": "Payment amount mismatch"}, status=400)
            if order.status == "pending":
                transition_order(order, "paid", note="Pembayaran Xendit diterima")
                OutboxEvent.objects.create(store=order.store, topic="payment.received", aggregate_id=str(order.id),
                    payload={"order_id": str(order.id), "phone": order.phone, "total": str(order.total)})
        elif order.status == "pending":
            try:
                transition_order(order, "cancelled", note="Sesi pembayaran Xendit kedaluwarsa")
            except InvalidOrderTransition:
                pass
    return JsonResponse({"received": True})
