import hashlib
import hmac
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from apps.orders.access import get_customer_order_or_404
from apps.orders.models import Order, PaymentEvent, PaymentTransaction
from apps.orders.services import InvalidOrderTransition, transition_order

from .models import OutboxEvent
from .payments import doku_signature


def demo_payment(request, order_id):
    if not request.store: raise Http404
    order = get_customer_order_or_404(request, order_id)
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
    if event_type not in {"payment_session.completed", "payment_session.failed",
            "payment_session.expired"} or not reference:
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
            state_mismatch = order.status not in {"pending", "paid"}
            if state_mismatch and not mismatch:
                mismatch = f"Order status {order.status} cannot accept payment"
            PaymentTransaction.objects.update_or_create(provider="xendit",
                external_id=data.get("payment_session_id") or order.payment_reference, kind="payment",
                defaults={"order": order, "status": "failed" if mismatch and not state_mismatch else "succeeded",
                    "provider_payment_id": data.get("payment_request_id", ""), "amount": amount,
                    "currency": currency or "IDR", "reconciled": not mismatch,
                    "mismatch_reason": mismatch, "payload": data})
            if mismatch:
                status_code = 200 if state_mismatch else 400
                return JsonResponse({"error": mismatch, "received": state_mismatch}, status=status_code)
            if order.status == "pending":
                transition_order(order, "paid", note="Pembayaran Xendit diterima")
                OutboxEvent.objects.create(store=order.store, topic="payment.received", aggregate_id=str(order.id),
                    payload={"order_id": str(order.id), "phone": order.phone, "total": str(order.total)})
        elif event_type == "payment_session.failed" and order.status == "pending":
            PaymentTransaction.objects.update_or_create(provider="xendit",
                external_id=data.get("payment_session_id") or order.payment_reference, kind="payment",
                defaults={"order": order, "status": "failed", "amount": order.total,
                    "currency": order.store.currency, "reconciled": True, "payload": data})
            transition_order(order, "payment_failed", note="Pembayaran Xendit gagal",
                release_inventory=True)
        elif order.status == "pending":
            try:
                transition_order(order, "cancelled", note="Sesi pembayaran Xendit kedaluwarsa")
            except InvalidOrderTransition:
                pass
    return JsonResponse({"received": True})


@csrf_exempt
def doku_webhook(request):
    if request.method != "POST": return HttpResponse(status=405)
    client_id = request.headers.get("Client-Id", "")
    request_id = request.headers.get("Request-Id", "")
    timestamp = request.headers.get("Request-Timestamp", "")
    received_signature = request.headers.get("Signature", "")
    if not settings.DOKU_CLIENT_ID or not settings.DOKU_SECRET_KEY:
        return JsonResponse({"error": "DOKU webhook is not configured"}, status=503)
    expected_signature = doku_signature(client_id, request_id, timestamp, request.path,
        request.body, settings.DOKU_SECRET_KEY)
    if (not hmac.compare_digest(client_id, settings.DOKU_CLIENT_ID) or not request_id or
            not timestamp or not hmac.compare_digest(received_signature, expected_signature)):
        return JsonResponse({"error": "Invalid webhook signature"}, status=401)
    try: payload = json.loads(request.body)
    except json.JSONDecodeError: return JsonResponse({"error": "Invalid JSON"}, status=400)
    invoice = str((payload.get("order") or {}).get("invoice_number", ""))
    transaction_data = payload.get("transaction") or {}
    status = transaction_data.get("status", "")
    if not invoice or status not in {"SUCCESS", "FAILED"}:
        return JsonResponse({"received": True})
    # DOKU Checkout lets a customer try a different channel after one channel fails.
    # A FAILED notification is therefore acknowledged but must not fail the order.
    if status == "FAILED":
        return JsonResponse({"received": True, "ignored": True})
    with transaction.atomic():
        try: order = Order.objects.select_for_update().get(
            payment_reference=invoice, payment_provider="doku")
        except Order.DoesNotExist: return JsonResponse({"error": "Order not found"}, status=404)
        _, created = PaymentEvent.objects.get_or_create(store=order.store, provider="doku",
            external_id=request_id, defaults={"event_type": status, "payload": payload})
        if not created: return JsonResponse({"received": True, "duplicate": True})
        external_id = transaction_data.get("original_request_id") or request_id
        if status == "SUCCESS":
            amount = Decimal(str((payload.get("order") or {}).get("amount", -1)))
            currency = (payload.get("order") or {}).get("currency", "IDR")
            mismatch = "" if amount == order.total and currency == "IDR" else "Amount or currency mismatch"
            state_mismatch = order.status not in {"pending", "paid"}
            if state_mismatch and not mismatch:
                mismatch = f"Order status {order.status} cannot accept payment"
            PaymentTransaction.objects.update_or_create(provider="doku", external_id=external_id,
                kind="payment", defaults={"order": order, "status": "failed" if mismatch and not state_mismatch else "succeeded",
                    "provider_payment_id": external_id, "amount": amount, "currency": currency,
                    "reconciled": not mismatch, "mismatch_reason": mismatch, "payload": payload})
            if mismatch:
                return JsonResponse({"error": mismatch, "received": state_mismatch},
                    status=200 if state_mismatch else 400)
            if order.status == "pending":
                transition_order(order, "paid", note="Pembayaran DOKU diterima")
                OutboxEvent.objects.create(store=order.store, topic="payment.received",
                    aggregate_id=str(order.id), payload={"order_id": str(order.id),
                        "phone": order.phone, "total": str(order.total)})
    return JsonResponse({"received": True})
