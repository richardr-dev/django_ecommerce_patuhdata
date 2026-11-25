"""Our own subscription payment page. The company sees the bill and picks a bank; we ask DOKU (direct API) for
a virtual account number and show it here with the amount, the deadline and how to pay. No DOKU page is opened.
DOKU's notification (same webhook, same ``SB`` reference) or a status check activates the plan.

Every DOKU method is listed on our page with its logo (``METHODS``). QRIS, e-wallets, cards, retail outlets,
paylater, and a bank whose direct virtual account DOKU refuses, open DOKU's own page limited to the one method
chosen (``payment_method_types``): those methods need DOKU's SNAP API, which this account does not use. A logo
is ``static/img/payments/<code>.svg|png|webp`` when present, else the name on the brand's colour. With
``PAYMENT_PROVIDER=demo`` the numbers are made up and the page offers a button that simulates the payment."""
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone

from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider
from apps.shops.models import AuditLog
from apps.shops.tenancy import platform_url

from .models import SubscriptionCheckout, SubscriptionInvoice

BANKS = {"bca": "BCA", "mandiri": "Mandiri", "bri": "BRI", "bni": "BNI", "permata": "Permata", "cimb": "CIMB Niaga"}
# What the company does in each bank's app, shown under the number (DOKU's own page is linked too when given).
STEPS = {
    "bca": ["Buka BCA mobile atau KlikBCA, pilih m-Transfer › BCA Virtual Account.",
        "Masukkan nomor virtual account di atas.", "Periksa nama dan jumlah, lalu konfirmasi dengan PIN."],
    "mandiri": ["Buka Livin' by Mandiri, pilih Bayar › Virtual Account.",
        "Masukkan nomor virtual account di atas.", "Periksa jumlah, lalu konfirmasi dengan PIN."],
    "bri": ["Buka BRImo, pilih BRIVA.", "Masukkan nomor virtual account di atas.",
        "Periksa jumlah, lalu konfirmasi dengan PIN."],
    "bni": ["Buka BNI Mobile Banking, pilih Transfer › Virtual Account Billing.",
        "Masukkan nomor virtual account di atas.", "Periksa jumlah, lalu konfirmasi dengan password transaksi."],
}
DEFAULT_STEPS = ["Buka aplikasi atau ATM bank Anda, pilih pembayaran Virtual Account.",
    "Masukkan nomor virtual account di atas.", "Periksa jumlah, lalu konfirmasi."]


class PaymentPageError(Exception):
    pass


@dataclass(frozen=True)
class Method:
    code: str
    label: str
    group: str
    doku_type: str        # DOKU Checkout's payment_method_types value
    colour: str           # badge colour when there is no logo file
    short: str = ""       # badge text (defaults to the label)


GROUPS = (("va", "Transfer virtual account", "Nomor tampil di halaman ini, bayar dari m-banking, internet banking atau ATM."),
    ("qris", "QRIS", "Scan dengan aplikasi bank atau e-wallet apa pun."),
    ("ewallet", "E-wallet", "Bayar dari aplikasi e-wallet."),
    ("card", "Kartu kredit / debit", "Visa, Mastercard, JCB."),
    ("retail", "Gerai retail", "Bayar tunai di kasir."),
    ("paylater", "Paylater", "Cicil lewat aplikasi paylater."))
METHODS = (
    Method("bca", "BCA", "va", "VIRTUAL_ACCOUNT_BCA", "#0060AF"),
    Method("mandiri", "Mandiri", "va", "VIRTUAL_ACCOUNT_BANK_MANDIRI", "#003D79"),
    Method("bri", "BRI", "va", "VIRTUAL_ACCOUNT_BRI", "#00529C"),
    Method("bni", "BNI", "va", "VIRTUAL_ACCOUNT_BNI", "#F05A22"),
    Method("permata", "Permata", "va", "VIRTUAL_ACCOUNT_BANK_PERMATA", "#00965E"),
    Method("cimb", "CIMB Niaga", "va", "VIRTUAL_ACCOUNT_BANK_CIMB", "#7A1315", "CIMB"),
    Method("bsi", "BSI", "va", "VIRTUAL_ACCOUNT_BSI", "#00A39D"),
    Method("danamon", "Danamon", "va", "VIRTUAL_ACCOUNT_BANK_DANAMON", "#F7941D"),
    Method("qris", "QRIS", "qris", "QRIS", "#1A1A1A"),
    Method("ovo", "OVO", "ewallet", "EMONEY_OVO", "#4C3494"),
    Method("dana", "DANA", "ewallet", "EMONEY_DANA", "#118EEA"),
    Method("shopeepay", "ShopeePay", "ewallet", "EMONEY_SHOPEE_PAY", "#EE4D2D"),
    Method("linkaja", "LinkAja", "ewallet", "EMONEY_LINKAJA", "#E82529"),
    Method("card", "Kartu kredit", "card", "CREDIT_CARD", "#1A1F71", "VISA"),
    Method("alfamart", "Alfamart", "retail", "ONLINE_TO_OFFLINE_ALFA", "#E31E24"),
    Method("indomaret", "Indomaret", "retail", "ONLINE_TO_OFFLINE_INDOMARET", "#0F4C9E"),
    Method("kredivo", "Kredivo", "paylater", "PEER_TO_PEER_KREDIVO", "#F26722"),
    Method("akulaku", "Akulaku", "paylater", "PEER_TO_PEER_AKULAKU", "#E2231A"),
    Method("indodana", "Indodana", "paylater", "PEER_TO_PEER_INDODANA", "#00A6A6"),
)
BY_CODE = {m.code: m for m in METHODS}
LOGO_DIR = Path(__file__).resolve().parents[2] / "static" / "img" / "payments"


def logo(code):
    """The method's logo file when one was added to static/img/payments, else ""."""
    for extension in ("svg", "png", "webp"):
        if (LOGO_DIR / f"{code}.{extension}").exists():
            return static(f"img/payments/{code}.{extension}")
    return ""


def offered():
    return [m for m in METHODS if m.code not in settings.DOKU_HIDDEN_METHODS]


def direct(method):
    """True when this method's number is made through DOKU's direct API and shown on our page."""
    return method.group == "va" and method.code in settings.DOKU_VA_BANKS and method.code in BANKS


def groups():
    """The picker: each group with its methods, logo and badge."""
    shown = offered()
    out = []
    for key, title, hint in GROUPS:
        items = [{"code": m.code, "label": m.label, "short": m.short or m.label, "colour": m.colour,
            "logo": logo(m.code), "here": direct(m)} for m in shown if m.group == key]
        if items:
            out.append({"key": key, "title": title, "hint": hint, "items": items})
    return out


def open_method(invoice, code, user):
    """What choosing a method does: ``("code", checkout)`` for a number shown here, ``("redirect", url)`` for
    DOKU's page set to that method. A direct virtual account DOKU refuses falls back to DOKU's page."""
    method = BY_CODE.get(code)
    if method is None or method not in offered():
        raise PaymentPageError("Pilih metode pembayaran yang tersedia.")
    if direct(method):
        try:
            return "code", open_virtual_account(invoice, method.code, user)
        except PaymentPageError:
            if settings.PAYMENT_PROVIDER == "demo":
                raise
    return "redirect", open_doku_checkout(invoice, user, method_types=[method.doku_type])


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
    if bank not in dict(banks()) or bank in settings.DOKU_HIDDEN_METHODS:
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


def open_doku_checkout(invoice, user, method_types=None):
    """DOKU's own page, limited to ``method_types`` when the buyer chose one here (when DOKU refuses that
    filter, the page with every method). Returns its URL."""
    provider = _ready(invoice)
    from .services import SUBSCRIPTION_PREFIX
    _keep_current(invoice)
    attempt = invoice.checkouts.count() + 1
    if provider == "demo":
        reference, url = f"demo-{invoice.id.hex}-{attempt}", reverse("billing-demo-pay", args=[invoice.id])
    else:
        reference = f"{SUBSCRIPTION_PREFIX}{invoice.id.hex[:24]}R{attempt}"
        doku = DokuPaymentProvider()
        args = {"invoice_number": reference, "amount": int(invoice.amount), "customer": _customer(invoice, user),
            "callback_url": platform_url(reverse("merchant-billing"))}
        try:
            try:
                url = doku.create_checkout(request_id=f"{invoice.id.hex}-{attempt}", method_types=method_types,
                    **args)
            except ProviderError:
                if not method_types:
                    raise
                url = doku.create_checkout(request_id=f"{invoice.id.hex}-{attempt}b", **args)
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
