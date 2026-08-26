import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings
from django.urls import reverse

from .http import ProviderError, request_json


@dataclass(frozen=True)
class PaymentSession:
    reference: str
    checkout_url: str
    expires_at: str | None = None


@dataclass(frozen=True)
class RefundResult:
    reference: str
    status: str
    payload: dict


class DemoPaymentProvider:
    """Local adapter used only when PAYMENT_PROVIDER=demo."""

    def create_payment(self, order):
        return PaymentSession(reference=f"demo-{order.id}", checkout_url=reverse("demo-payment", args=[order.id]))

    def refund(self, payment, refund):
        return RefundResult(reference=f"demo-refund-{refund.id}", status="succeeded",
            payload={"id": f"demo-refund-{refund.id}", "status": "SUCCEEDED"})


class XenditPaymentProvider:
    def __init__(self):
        if not settings.XENDIT_SECRET_KEY:
            raise ProviderError("Xendit secret key is not configured.")

    def create_payment(self, order):
        auth = base64.b64encode(f"{settings.XENDIT_SECRET_KEY}:".encode()).decode()
        names = [re.sub(r"[^A-Za-z0-9 ]", "", part) for part in order.customer_name.split(maxsplit=1)]
        customer = {"reference_id": f"customer{order.id.hex}", "type": "INDIVIDUAL",
            "mobile_number": f"+62{order.phone[1:]}", "individual_detail": {"given_names": names[0] or "Customer"}}
        if len(names) > 1: customer["individual_detail"]["surname"] = names[1]
        if order.email: customer["email"] = order.email
        payload = {"reference_id": str(order.id), "session_type": "PAY", "mode": "PAYMENT_LINK",
            "amount": int(order.total), "currency": "IDR", "country": "ID", "locale": "id",
            "capture_method": "AUTOMATIC", "allow_save_payment_method": "DISABLED", "customer": customer,
            "description": f"Pesanan {order.store.name} #{str(order.id)[:8]}",
            "metadata": {"store_id": str(order.store_id), "order_id": str(order.id)}}
        if settings.PUBLIC_BASE_URL.startswith("https://"):
            return_url = (f"{settings.PUBLIC_BASE_URL}{reverse('order-detail', args=[order.id])}"
                f"?token={order.access_token}")
            payload.update({"success_return_url": return_url, "cancel_return_url": return_url})
        data = request_json(f"{settings.XENDIT_API_URL}/sessions", method="POST",
            headers={"Authorization": f"Basic {auth}", "api-version": "2024-11-11",
                "Idempotency-Key": order.idempotency_key}, payload=payload)
        if not data.get("payment_session_id") or not data.get("payment_link_url"):
            raise ProviderError("Xendit returned an incomplete payment session.")
        return PaymentSession(reference=data["payment_session_id"], checkout_url=data["payment_link_url"],
            expires_at=data.get("expires_at"))

    def refund(self, payment, refund):
        if not payment.provider_payment_id:
            raise ProviderError("Xendit payment request ID is unavailable; reconcile the payment first.")
        auth = base64.b64encode(f"{settings.XENDIT_SECRET_KEY}:".encode()).decode()
        payload = {"reference_id": str(refund.id), "payment_request_id": payment.provider_payment_id,
            "currency": payment.currency, "amount": int(refund.amount), "reason": refund.reason,
            "metadata": {"order_id": str(refund.order_id), "store_id": str(refund.order.store_id)}}
        data = request_json(f"{settings.XENDIT_API_URL}/refunds", method="POST",
            headers={"Authorization": f"Basic {auth}", "Idempotency-Key": str(refund.id)}, payload=payload)
        status = {"SUCCEEDED": "succeeded", "FAILED": "failed"}.get(data.get("status"), "pending")
        return RefundResult(reference=data.get("id", ""), status=status, payload=data)


def doku_signature(client_id, request_id, timestamp, target, body, secret_key):
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    component = (f"Client-Id:{client_id}\nRequest-Id:{request_id}\n"
        f"Request-Timestamp:{timestamp}\nRequest-Target:{target}\nDigest:{digest}")
    signature = base64.b64encode(hmac.new(secret_key.encode(), component.encode(), hashlib.sha256).digest()).decode()
    return f"HMACSHA256={signature}"


class DokuPaymentProvider:
    target = "/checkout/v1/payment"

    def __init__(self):
        if not settings.DOKU_CLIENT_ID or not settings.DOKU_SECRET_KEY:
            raise ProviderError("DOKU client ID and secret key are not configured.")

    def create_payment(self, order):
        invoice = f"NK{order.id.hex[:26]}"
        payload = {"order": {"amount": int(order.total), "invoice_number": invoice,
            "currency": "IDR"},
            "payment": {"payment_due_date": settings.DOKU_PAYMENT_DUE_MINUTES},
            "customer": {"name": order.customer_name, "phone": order.phone}}
        if order.email:
            payload["customer"]["email"] = order.email
        if settings.PUBLIC_BASE_URL.startswith("https://"):
            payload["callbacks"] = {"url": (f"{settings.PUBLIC_BASE_URL}"
                f"{reverse('order-detail', args=[order.id])}?token={order.access_token}")}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        request_id = order.idempotency_key[:128]
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"Client-Id": settings.DOKU_CLIENT_ID, "Request-Id": request_id,
            "Request-Timestamp": timestamp, "Signature": doku_signature(settings.DOKU_CLIENT_ID,
                request_id, timestamp, self.target, body, settings.DOKU_SECRET_KEY)}
        data = request_json(f"{settings.DOKU_API_URL}{self.target}", method="POST", headers=headers,
            encoded_payload=body)
        payment = (data.get("response") or {}).get("payment") or {}
        if not payment.get("url"):
            raise ProviderError("DOKU returned an incomplete payment session.")
        return PaymentSession(reference=invoice, checkout_url=payment["url"])

    def refund(self, payment, refund):
        raise ProviderError(
            "Refund DOKU harus dikonfigurasi sesuai channel pembayaran yang diaktifkan."
        )


PROVIDERS = {"demo": DemoPaymentProvider, "xendit": XenditPaymentProvider,
    "doku": DokuPaymentProvider}


def get_payment_provider(name):
    try: return PROVIDERS[name]()
    except KeyError as error: raise ValueError(f"Payment provider is not configured: {name}") from error
