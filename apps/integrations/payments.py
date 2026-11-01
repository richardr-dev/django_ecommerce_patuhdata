import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.urls import reverse

from apps.shops.tenancy import store_origin

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


def _order_return_url(order):
    """Where the gateway sends the customer back: the order page on the store's own host."""
    return (f"{store_origin(order.store)}{reverse('order-detail', args=[order.id])}"
        f"?token={order.access_token}")


class DemoPaymentProvider:
    """Local adapter used only when PAYMENT_PROVIDER=demo."""

    @staticmethod
    def reference_for(order):
        return f"demo-{order.id}"

    def create_payment(self, order):
        if not settings.ALLOW_DEMO_PAYMENTS:
            raise ProviderError("Demo payments are disabled; configure DOKU for production.")
        return PaymentSession(reference=self.reference_for(order),
            checkout_url=reverse("demo-payment", args=[order.id]))

    def refund(self, payment, refund):
        return RefundResult(reference=f"demo-refund-{refund.id}", status="succeeded",
            payload={"id": f"demo-refund-{refund.id}", "status": "SUCCEEDED"})


class XenditPaymentProvider:
    def __init__(self, secret_key=None):
        """``secret_key`` is a seller's own key; None uses the platform account."""
        self.secret_key = settings.XENDIT_SECRET_KEY if secret_key is None else secret_key
        if not self.secret_key:
            raise ProviderError("Xendit secret key is not configured.")

    def create_payment(self, order):
        auth = base64.b64encode(f"{self.secret_key}:".encode()).decode()
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
        return_url = _order_return_url(order)
        if return_url.startswith("https://"):
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
        auth = base64.b64encode(f"{self.secret_key}:".encode()).decode()
        payload = {"reference_id": str(refund.id), "payment_request_id": payment.provider_payment_id,
            "currency": payment.currency, "amount": int(refund.amount), "reason": refund.reason,
            "metadata": {"order_id": str(refund.order_id), "store_id": str(refund.order.store_id)}}
        data = request_json(f"{settings.XENDIT_API_URL}/refunds", method="POST",
            headers={"Authorization": f"Basic {auth}", "Idempotency-Key": str(refund.id)}, payload=payload)
        status = {"SUCCEEDED": "succeeded", "FAILED": "failed"}.get(data.get("status"), "pending")
        return RefundResult(reference=data.get("id", ""), status=status, payload=data)


def doku_signature(client_id, request_id, timestamp, target, body, secret_key):
    """DOKU HMAC signature; GET requests (body=None) omit the Digest component."""
    component = (f"Client-Id:{client_id}\nRequest-Id:{request_id}\n"
        f"Request-Timestamp:{timestamp}\nRequest-Target:{target}")
    if body is not None:
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
        component += f"\nDigest:{digest}"
    signature = base64.b64encode(hmac.new(secret_key.encode(), component.encode(), hashlib.sha256).digest()).decode()
    return f"HMACSHA256={signature}"


@dataclass(frozen=True)
class PaymentStatus:
    """Normalized gateway status: paid, unpaid (safe to cancel) or pending (unknown yet)."""
    state: str
    amount: Decimal | None = None
    currency: str = "IDR"
    external_id: str = ""
    payload: dict | None = None


class DokuPaymentProvider:
    target = "/checkout/v1/payment"
    status_target = "/orders/v1/status/{invoice}"

    def __init__(self, client_id=None, secret_key=None):
        """Credentials of a seller's own account; None uses the platform account."""
        self.client_id = settings.DOKU_CLIENT_ID if client_id is None else client_id
        self.secret_key = settings.DOKU_SECRET_KEY if secret_key is None else secret_key
        if not self.client_id or not self.secret_key:
            raise ProviderError("DOKU client ID and secret key are not configured.")

    @staticmethod
    def reference_for(order):
        return f"NK{order.id.hex[:26]}"

    def _headers(self, request_id, target, body):
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"Client-Id": self.client_id, "Request-Id": request_id,
            "Request-Timestamp": timestamp, "Signature": doku_signature(self.client_id,
                request_id, timestamp, target, body, self.secret_key)}

    def create_checkout(self, *, invoice_number, amount, customer, request_id, callback_url=""):
        """Create a hosted checkout page and return its URL."""
        payload = {"order": {"amount": amount, "invoice_number": invoice_number, "currency": "IDR"},
            "payment": {"payment_due_date": settings.DOKU_PAYMENT_DUE_MINUTES},
            "customer": customer}
        if callback_url.startswith("https://"):
            payload["callbacks"] = {"url": callback_url}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        data = request_json(f"{settings.DOKU_API_URL}{self.target}", method="POST",
            headers=self._headers(request_id, self.target, body), encoded_payload=body)
        payment = (data.get("response") or {}).get("payment") or {}
        if not payment.get("url"):
            raise ProviderError("DOKU returned an incomplete payment session.")
        return payment["url"]

    def create_payment(self, order):
        invoice = self.reference_for(order)
        customer = {"name": order.customer_name, "phone": order.phone}
        if order.email:
            customer["email"] = order.email
        checkout_url = self.create_checkout(invoice_number=invoice, amount=int(order.total),
            customer=customer, request_id=order.idempotency_key[:128],
            callback_url=_order_return_url(order))
        # DOKU does not return an expiry; reserve stock for the due date plus a short grace
        # period so a late notification still finds the order pending.
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.DOKU_PAYMENT_DUE_MINUTES + 5)
        return PaymentSession(reference=invoice, checkout_url=checkout_url,
            expires_at=expires_at.isoformat())

    def fetch_status(self, order):
        target = self.status_target.format(invoice=order.payment_reference)
        try:
            data = request_json(f"{settings.DOKU_API_URL}{target}",
                headers=self._headers(uuid.uuid4().hex, target, None))
        except ProviderError as error:
            # DOKU answers 404 when the customer never selected a payment channel.
            if "HTTP 404" in str(error):
                return PaymentStatus(state="unpaid", payload={"error": str(error)})
            raise
        transaction_data = data.get("transaction") or {}
        status = str(transaction_data.get("status", "")).upper()
        order_data = data.get("order") or {}
        if status == "SUCCESS":
            return PaymentStatus(state="paid", amount=Decimal(str(order_data.get("amount", -1))),
                currency=order_data.get("currency", "IDR"),
                external_id=transaction_data.get("original_request_id") or order.payment_reference,
                payload=data)
        if status in {"FAILED", "EXPIRED"}:
            return PaymentStatus(state="unpaid", payload=data)
        return PaymentStatus(state="pending", payload=data)

    def refund(self, payment, refund):
        raise ProviderError(
            "Refund DOKU untuk channel ini belum tersedia via API. Gunakan refund transfer manual."
        )


class BankTransferProvider:
    """The buyer transfers straight to the seller's bank account and the seller confirms receipt
    in the back office. Needs no gateway account, so a new store can sell on day one."""

    @staticmethod
    def reference_for(order):
        return f"TF{order.id.hex[:10].upper()}"

    def create_payment(self, order):
        expires_at = datetime.now(UTC) + timedelta(hours=settings.BANK_TRANSFER_DUE_HOURS)
        return PaymentSession(reference=self.reference_for(order),
            checkout_url=_order_return_url(order), expires_at=expires_at.isoformat())

    def refund(self, payment, refund):
        raise ProviderError("Pembayaran transfer bank direfund manual: pilih refund transfer manual.")


PROVIDERS = {"demo": DemoPaymentProvider, "xendit": XenditPaymentProvider,
    "doku": DokuPaymentProvider, "bank_transfer": BankTransferProvider}

GATEWAY_LABELS = {
    "doku": ("DOKU Checkout", "QRIS, virtual account, e-wallet, retail outlet, atau kartu"),
    "xendit": ("Xendit", "QRIS, virtual account, e-wallet, atau kartu"),
    "demo": ("Pembayaran demo", "Mode pengembangan: pesanan ditandai lunas tanpa transaksi nyata"),
}


@dataclass(frozen=True)
class PaymentOption:
    code: str  # the checkout choice, stored as Order.payment_method
    provider: str
    store_account: bool
    label: str
    description: str


def store_payment_settings(store):
    from .models import StorePaymentSettings

    return StorePaymentSettings.objects.filter(store=store).first()


def platform_gateway_ready():
    if settings.PAYMENT_PROVIDER == "doku":
        return bool(settings.DOKU_CLIENT_ID and settings.DOKU_SECRET_KEY)
    if settings.PAYMENT_PROVIDER == "xendit":
        return bool(settings.XENDIT_SECRET_KEY)
    return False


def payment_options(store):
    """How a buyer can pay this store, best first. Money goes to the seller's own accounts; the
    platform account is offered only for the demo or when PLATFORM_COLLECTS_PAYMENTS is set."""
    account = store_payment_settings(store)
    options = []
    if account and account.gateway_ready:
        options.append(PaymentOption("gateway", account.gateway, True, *GATEWAY_LABELS[account.gateway]))
    elif (settings.PAYMENT_PROVIDER == "demo" and settings.ALLOW_DEMO_PAYMENTS) or (
            settings.PLATFORM_COLLECTS_PAYMENTS and platform_gateway_ready()):
        options.append(PaymentOption("gateway", settings.PAYMENT_PROVIDER, False,
            *GATEWAY_LABELS.get(settings.PAYMENT_PROVIDER, (settings.PAYMENT_PROVIDER, ""))))
    if account and account.bank_transfer_ready:
        options.append(PaymentOption("bank_transfer", "bank_transfer", True, "Transfer bank",
            f"Transfer ke {account.bank_name} a.n. {account.bank_account_holder}; pesanan diproses "
            "setelah penjual mengonfirmasi dana masuk"))
    return options


def reserved_reference(provider, order):
    """Reference known before the gateway call, so a lost response can still be reconciled."""
    reference_for = getattr(provider, "reference_for", None)
    return reference_for(order) if reference_for else ""


def get_payment_provider(name, *, store=None, store_account=False):
    """The adapter for ``name``: on the seller's own account when ``store_account`` is set,
    otherwise on the platform account (subscriptions, the demo, and older orders)."""
    if name not in PROVIDERS:
        raise ValueError(f"Payment provider is not configured: {name}")
    if not store_account or name == "bank_transfer":
        return PROVIDERS[name]()
    account = store_payment_settings(store)
    if not account or account.gateway != name:
        raise ProviderError(f"Akun {name} milik toko belum terhubung.")
    if name == "doku":
        return DokuPaymentProvider(client_id=account.doku_client_id, secret_key=account.secret_key)
    return XenditPaymentProvider(secret_key=account.secret_key)


def order_payment_provider(order):
    return get_payment_provider(order.payment_provider, store=order.store,
        store_account=order.paid_to_store_account)
