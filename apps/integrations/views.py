import hashlib
import hmac
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from apps.billing.services import SUBSCRIPTION_PREFIX, handle_doku_notification
from apps.orders.access import get_customer_order_or_404
from apps.orders.models import Order, PaymentEvent, PaymentTransaction
from apps.orders.services import InvalidOrderTransition, transition_order

from .models import OutboxEvent, StorePaymentSettings
from .payments import TEST_INVOICE_PREFIX, doku_signature
from .reconciliation import confirm_payment


def demo_payment(request, order_id):
    if not request.store or not settings.ALLOW_DEMO_PAYMENTS: raise Http404
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


def _store_account(webhook_key, gateway):
    """The store whose own gateway account sends to this notification URL, or 404."""
    account = StorePaymentSettings.objects.select_related("store").filter(
        webhook_key=webhook_key, gateway=gateway).first()
    if not account or not account.gateway_ready: raise Http404
    return account


@csrf_exempt
def xendit_webhook(request, webhook_key=None):
    """Platform-account notifications at the bare URL; a seller's own account at its keyed URL."""
    if request.method != "POST": return HttpResponse(status=405)
    if webhook_key:
        account = _store_account(webhook_key, "xendit")
        expected = account.webhook_token
        orders = Order.objects.filter(store=account.store, paid_to_store_account=True)
    else:
        expected = settings.XENDIT_WEBHOOK_VERIFICATION_TOKEN
        orders = Order.objects.filter(paid_to_store_account=False)
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
        try: order = orders.select_for_update().get(id=reference, payment_provider="xendit")
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
def doku_webhook(request, webhook_key=None):
    """Platform-account notifications at the bare URL; a seller's own account at its keyed URL."""
    if request.method != "POST": return HttpResponse(status=405)
    client_id = request.headers.get("Client-Id", "")
    request_id = request.headers.get("Request-Id", "")
    timestamp = request.headers.get("Request-Timestamp", "")
    received_signature = request.headers.get("Signature", "")
    if webhook_key:
        account = _store_account(webhook_key, "doku")
        expected_client_id, secret_key = account.doku_client_id, account.secret_key
        orders = Order.objects.filter(store=account.store, paid_to_store_account=True)
    else:
        if not settings.DOKU_CLIENT_ID or not settings.DOKU_SECRET_KEY:
            return JsonResponse({"error": "DOKU webhook is not configured"}, status=503)
        expected_client_id, secret_key = settings.DOKU_CLIENT_ID, settings.DOKU_SECRET_KEY
        orders = Order.objects.filter(paid_to_store_account=False)
    expected_signature = doku_signature(client_id, request_id, timestamp, request.path,
        request.body, secret_key)
    if (not hmac.compare_digest(client_id, expected_client_id) or not request_id or
            not timestamp or not hmac.compare_digest(received_signature, expected_signature)):
        return JsonResponse({"error": "Invalid webhook signature"}, status=401)
    try: payload = json.loads(request.body)
    except json.JSONDecodeError: return JsonResponse({"error": "Invalid JSON"}, status=400)
    invoice = str((payload.get("order") or {}).get("invoice_number", ""))
    transaction_data = payload.get("transaction") or {}
    status = transaction_data.get("status", "")
    if invoice.startswith(TEST_INVOICE_PREFIX):
        return JsonResponse({"received": True, "test": True})
    if not invoice or status not in {"SUCCESS", "FAILED"}:
        return JsonResponse({"received": True})
    # DOKU Checkout lets a customer try a different channel after one channel fails.
    # A FAILED notification is therefore acknowledged but must not fail the order.
    if status == "FAILED":
        return JsonResponse({"received": True, "ignored": True})
    if invoice.startswith(SUBSCRIPTION_PREFIX) and not webhook_key:
        body, status_code = handle_doku_notification(invoice, request_id, payload)
        return JsonResponse(body, status=status_code)
    with transaction.atomic():
        try: order = orders.select_for_update().get(
            payment_reference=invoice, payment_provider="doku")
        except Order.DoesNotExist: return JsonResponse({"error": "Order not found"}, status=404)
        _, created = PaymentEvent.objects.get_or_create(store=order.store, provider="doku",
            external_id=request_id, defaults={"event_type": status, "payload": payload})
        if not created: return JsonResponse({"received": True, "duplicate": True})
        external_id = transaction_data.get("original_request_id") or request_id
        order_data = payload.get("order") or {}
        mismatch = confirm_payment(order, provider="doku", external_id=external_id,
            provider_payment_id=external_id, amount=order_data.get("amount", -1),
            currency=order_data.get("currency", "IDR"), payload=payload,
            note="Pembayaran DOKU diterima")
        if mismatch:
            received = mismatch.startswith("Order status")
            return JsonResponse({"error": mismatch, "received": received},
                status=200 if received else 400)
    return JsonResponse({"received": True})
