import base64
import re
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse

from .http import ProviderError, request_json


@dataclass(frozen=True)
class PaymentSession:
    reference: str
    checkout_url: str
    expires_at: str | None = None


class DemoPaymentProvider:
    """Local adapter used only when PAYMENT_PROVIDER=demo."""

    def create_payment(self, order):
        return PaymentSession(reference=f"demo-{order.id}", checkout_url=reverse("demo-payment", args=[order.id]))


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
            return_url = f"{settings.PUBLIC_BASE_URL}{reverse('order-detail', args=[order.id])}"
            payload.update({"success_return_url": return_url, "cancel_return_url": return_url})
        data = request_json(f"{settings.XENDIT_API_URL}/sessions", method="POST",
            headers={"Authorization": f"Basic {auth}", "api-version": "2024-11-11"}, payload=payload)
        if not data.get("payment_session_id") or not data.get("payment_link_url"):
            raise ProviderError("Xendit returned an incomplete payment session.")
        return PaymentSession(reference=data["payment_session_id"], checkout_url=data["payment_link_url"],
            expires_at=data.get("expires_at"))


PROVIDERS = {"demo": DemoPaymentProvider, "xendit": XenditPaymentProvider}


def get_payment_provider(name):
    try: return PROVIDERS[name]()
    except KeyError as error: raise ValueError(f"Payment provider is not configured: {name}") from error
