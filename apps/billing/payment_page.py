"""Our own subscription payment page. The company sees the bill and picks a bank; we ask DOKU (direct API) for
a virtual account number and show it here with the amount, the deadline and how to pay. No DOKU page is opened.
DOKU's notification (same webhook, same ``SB`` reference) or a status check activates the plan.

"Metode lain" (QRIS, e-wallet, card) still opens DOKU's own page: those methods need DOKU's SNAP API and a
registered QRIS merchant, which this account does not have yet. With ``PAYMENT_PROVIDER=demo`` the numbers are
made up and the page offers a button that simulates the payment."""
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider
from apps.shops.models import AuditLog
from apps.shops.tenancy import platform_url

from .models import SubscriptionCheckout, SubscriptionInvoice

BANKS = {"bca": "BCA", "mandiri": "Mandiri", "bri": "BRI", "bni": "BNI", "permata": "Permata", "cimb": "CIMB Niaga",
    "doku": "DOKU"}
# What the company does in each bank's app, shown under the number (DOKU's own page is linked too when given).
STEPS = {
    "bca": ["Buka BCA mobile atau KlikBCA, pilih m-Transfer › BCA Virtual Account.",
        "Masukkan nomor virtual account di atas.", "Periksa nama dan jumlah, lalu konfirmasi dengan PIN."],
    "mandiri": ["Buka Livin' by Mandiri, pilih Bayar › Buat pembayaran baru › Multipayment.",
        "Pilih penyedia DOKU, masukkan nomor virtual account.", "Periksa jumlah, lalu konfirmasi dengan PIN."],
    "bri": ["Buka BRImo, pilih BRIVA.", "Masukkan nomor virtual account di atas.",
        "Periksa jumlah, lalu konfirmasi dengan PIN."],
    "bni": ["Buka BNI Mobile Banking, pilih Transfer › Virtual Account Billing.",
        "Masukkan nomor virtual account di atas.", "Periksa jumlah, lalu konfirmasi dengan password transaksi."],
}
DEFAULT_STEPS = ["Buka aplikasi atau ATM bank Anda, pilih pembayaran Virtual Account.",
    "Masukkan nomor virtual account di atas.", "Periksa jumlah, lalu konfirmasi."]


class PaymentPageError(Exception):
    pass


def page_url(invoice):
    return reverse("merchant-billing-payment", args=[invoice.id])


def banks():
    return [(code, BANKS[code]) for code in settings.DOKU_VA_BANKS if code in BANKS]


def _ready(invoice):
    if invoice.status != "pending":
        raise PaymentPageError("Tagihan ini sudah tidak menunggu pembayaran.")
    provider = settings.PAYMENT_PROVIDER
    if provider not in {"doku", "demo"} or (provider == "demo" and not settings.ALLOW_DEMO_PAYMENTS):
        raise PaymentPageError("Pembayaran langganan belum dikonfigurasi. Hubungi pengelola platform.")
    return provider


def open_code(invoice):
    """The virtual account still payable for this bill, or None."""
    return invoice.checkouts.filter(method__startswith="va_", expires_at__gt=timezone.now()).order_by(
        "-created_at").first()


def _keep_current(invoice):
    """The reference made at checkout stays matchable after a new one replaces it."""
    if invoice.payment_reference:
        SubscriptionCheckout.objects.get_or_create(reference=invoice.payment_reference,
            defaults={"invoice": invoice, "checkout_url": invoice.checkout_url})


def _customer(invoice, user):
    customer = {"name": invoice.store.name[:100]}
    if user.email:
        customer["email"] = user.email
    return customer


def open_virtual_account(invoice, bank, user):
    """A virtual account number for this bill at ``bank``; the open one is reused while it is valid."""
    provider = _ready(invoice)
    if bank not in dict(banks()):
        raise PaymentPageError("Pilih bank yang tersedia.")
    current = open_code(invoice)
    if current and current.method == f"va_{bank}":
        return current
    from .services import SUBSCRIPTION_PREFIX
    _keep_current(invoice)
    attempt = invoice.checkouts.count() + 1
    minutes = settings.DOKU_PAYMENT_DUE_MINUTES
    reference = f"{SUBSCRIPTION_PREFIX}{invoice.id.hex[:24]}V{attempt}"
    if provider == "demo":
        code = {"number": f"8808{invoice.id.int % 10**12:012d}", "instructions_url": "",
            "expires_at": timezone.now() + timedelta(minutes=minutes)}
    else:
        try:
            code = DokuPaymentProvider().create_virtual_account(bank, invoice_number=reference,
                amount=int(invoice.amount), customer=_customer(invoice, user), request_id=f"{invoice.id.hex}-v{attempt}",
                minutes=minutes, info=f"Langganan {invoice.plan.name}")
        except ProviderError as error:
            raise PaymentPageError(f"Nomor virtual account {BANKS[bank]} belum dapat dibuat. Coba bank lain atau "
                "metode lain.") from error
    checkout = SubscriptionCheckout.objects.create(invoice=invoice, reference=reference, method=f"va_{bank}",
        pay_code=code["number"], instructions_url=code["instructions_url"][:500],
        expires_at=code["expires_at"] or timezone.now() + timedelta(minutes=minutes),
        checkout_url=page_url(invoice))
    SubscriptionInvoice.objects.filter(id=invoice.id).update(provider=provider, payment_reference=reference)
    AuditLog.objects.create(store=invoice.store, actor=user, action="billing.virtual_account_opened",
        object_type="SubscriptionInvoice", object_id=str(invoice.id), details={"bank": bank, "attempt": attempt})
    return checkout


def open_doku_checkout(invoice, user):
    """DOKU's own page for the methods our page does not offer (QRIS, e-wallet, card). Returns its URL."""
    provider = _ready(invoice)
    from .services import SUBSCRIPTION_PREFIX
    _keep_current(invoice)
    attempt = invoice.checkouts.count() + 1
    if provider == "demo":
        reference, url = f"demo-{invoice.id.hex}-{attempt}", reverse("billing-demo-pay", args=[invoice.id])
    else:
        reference = f"{SUBSCRIPTION_PREFIX}{invoice.id.hex[:24]}R{attempt}"
        try:
            url = DokuPaymentProvider().create_checkout(invoice_number=reference, amount=int(invoice.amount),
                customer=_customer(invoice, user), request_id=f"{invoice.id.hex}-{attempt}",
                callback_url=platform_url(reverse("merchant-billing")))
        except ProviderError as error:
            raise PaymentPageError("Halaman pembayaran belum dapat dibuat. Coba lagi beberapa saat.") from error
    SubscriptionCheckout.objects.create(invoice=invoice, reference=reference, checkout_url=url)
    SubscriptionInvoice.objects.filter(id=invoice.id).update(provider=provider, payment_reference=reference,
        checkout_url=url)
    AuditLog.objects.create(store=invoice.store, actor=user, action="billing.payment_page_opened",
        object_type="SubscriptionInvoice", object_id=str(invoice.id), details={"attempt": attempt})
    return url


def steps_for(method):
    return STEPS.get(method.removeprefix("va_"), DEFAULT_STEPS)
