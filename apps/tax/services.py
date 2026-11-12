import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounting.ledger import INPUT_VAT, VAT, account_balances, money
from apps.orders.models import Order

from .models import Invoice, InvoiceItem, InvoicePayment, InvoiceSequence, TaxProfile

logger = logging.getLogger(__name__)

# PP 55/2022: 0.5% of monthly gross turnover; individuals pay nothing on the first Rp500 million
# of each tax year.
UMKM_RATE = Decimal("0.005")
INDIVIDUAL_EXEMPTION = Decimal(500_000_000)
UMKM_TURNOVER_LIMIT = Decimal(4_800_000_000)


class InvoiceError(ValueError):
    pass


def normalize_npwp(value):
    """16-digit NPWP (or NIK). Legacy 15-digit NPWP numbers become 16 digits with a leading 0."""
    digits = "".join(character for character in value or "" if character.isdigit())
    if len(digits) == 15:
        digits = "0" + digits
    if digits and len(digits) != 16:
        raise ValueError("NPWP harus 16 digit (atau NPWP lama 15 digit).")
    return digits


def tax_profile(store):
    return TaxProfile.objects.filter(store=store).first() or TaxProfile(store=store)


def vat_for(store, tax_base, *, is_pkp=None):
    """PPN to charge on a sale of ``tax_base``: ``(rate, amount)``. Only a PKP seller charges
    PPN, at the store's rate; everyone else charges none."""
    if is_pkp is None:
        is_pkp = tax_profile(store).is_pkp
    if not is_pkp or not store.tax_rate or tax_base <= 0:
        return Decimal(0), Decimal(0)
    return store.tax_rate, money(tax_base * store.tax_rate / 100)


def _tax_rate(store, tax_base, tax):
    """The rate the order was charged: the store's current rate if it reproduces the tax,
    otherwise the rate implied by the amounts (the store changed its rate since)."""
    if not tax or not tax_base:
        return Decimal(0)
    if money(tax_base * store.tax_rate / 100) == tax:
        return store.tax_rate
    return (tax * 100 / tax_base).quantize(Decimal("0.01"))


def _create_invoice_items(invoice, order):
    """Copy OrderItems into InvoiceItems. Called inside the same transaction as the invoice."""
    from apps.orders.models import OrderItem
    for item in OrderItem.objects.filter(order=order).select_related("product"):
        InvoiceItem.objects.create(
            invoice=invoice,
            product=item.product,
            description=item.name,
            sku=item.sku or "",
            quantity=item.quantity,
            unit_price=item.unit_price,
            discount=getattr(item, "discount", Decimal(0)),
            line_total=item.line_total,
        )


def _next_invoice_number(store, issued_on, profile):
    sequence, _ = InvoiceSequence.objects.get_or_create(store=store, year=issued_on.year)
    sequence = InvoiceSequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"{profile.invoice_prefix}/{issued_on.year}/{sequence.last_number:06d}"


def issue_invoice(order):
    """Issue the invoice of a paid order once. Returns (invoice, created)."""
    existing = Invoice.objects.filter(order=order).first()
    if existing:
        return existing, False
    payment = order.payment_transactions.filter(kind="payment", status="succeeded",
        reconciled=True).order_by("id").first()
    if not payment:
        raise InvoiceError("Pesanan belum dibayar.")
    store, issued_on = order.store, timezone.localdate(payment.updated_at)
    profile = tax_profile(store)
    tax_base = max(Decimal(0), order.subtotal - order.discount)
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(store=store, order=order,
                number=_next_invoice_number(store, issued_on, profile),
                issued_on=issued_on, status=Invoice.STATUS_PAID,
                seller_name=profile.legal_name or store.name,
                seller_npwp=profile.npwp, seller_address=profile.address,
                seller_is_pkp=profile.is_pkp, buyer_name=order.customer_name,
                buyer_address=order.shipping_address, subtotal=order.subtotal,
                discount=order.discount, tax_base=tax_base,
                tax_rate=_tax_rate(store, tax_base, order.tax), tax=order.tax,
                shipping=order.shipping_cost,
                other_charges=order.total - tax_base - order.tax - order.shipping_cost,
                total=order.total)
            _create_invoice_items(invoice, order)
    except IntegrityError:
        existing = Invoice.objects.filter(order=order).first()
        if existing:
            return existing, False
        raise
    return invoice, True


def issue_invoice_for_order(order, issued_on=None, notes=""):
    """Issue an invoice for any order regardless of payment gateway status.
    Used for manual orders, marketplace imports, and mitra sell-through."""
    existing = Invoice.objects.filter(order=order).first()
    if existing:
        return existing, False

    store = order.store
    profile = tax_profile(store)
    if issued_on is None:
        issued_on = timezone.localdate()
    tax_base = max(Decimal(0), order.subtotal - order.discount)

    # Determine status from order status
    paid_statuses = {"paid", "confirmed", "delivered", "completed"}
    inv_status = Invoice.STATUS_PAID if order.status in paid_statuses else Invoice.STATUS_ISSUED

    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                store=store, order=order,
                number=_next_invoice_number(store, issued_on, profile),
                issued_on=issued_on, status=inv_status,
                seller_name=profile.legal_name or store.name,
                seller_npwp=profile.npwp, seller_address=profile.address,
                seller_is_pkp=profile.is_pkp,
                buyer_name=order.customer_name,
                buyer_address=order.shipping_address or "",
                subtotal=order.subtotal, discount=order.discount,
                tax_base=tax_base,
                tax_rate=_tax_rate(store, tax_base, order.tax),
                tax=order.tax, shipping=order.shipping_cost,
                other_charges=max(Decimal(0),
                    order.total - tax_base - order.tax - order.shipping_cost),
                total=order.total, notes=notes,
            )
            _create_invoice_items(invoice, order)
    except IntegrityError:
        existing = Invoice.objects.filter(order=order).first()
        if existing:
            return existing, False
        raise
    from .posting import sync_invoice_safely
    sync_invoice_safely(invoice)
    return invoice, True


def record_payment(invoice, amount, paid_on, method, reference="", note="", user=None,
        bank_account=None, receipt=None):
    """Record a payment against an invoice, book it, and update the invoice status."""
    from apps.accounting.ledger import LedgerError

    from .posting import booked_by_invoice, post_invoice, post_invoice_payment, refresh_status

    amount = Decimal(str(amount))
    if amount <= 0:
        raise InvoiceError("Jumlah pembayaran harus lebih dari nol.")
    if invoice.status in {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}:
        raise InvoiceError("Invoice ini tidak bisa menerima pembayaran.")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if amount > invoice.amount_due:
            raise InvoiceError("Pembayaran melebihi sisa tagihan "
                f"(Rp{invoice.amount_due:,.0f}).".replace(",", "."))
        payment = InvoicePayment.objects.create(
            invoice=invoice, paid_on=paid_on, amount=amount,
            method=method, reference=reference, note=note, recorded_by=user,
            bank_account=bank_account, receipt=receipt,
        )
        try:
            if booked_by_invoice(invoice):
                post_invoice(invoice, actor=user)  # invoices older than ledger posting
                post_invoice_payment(payment, strict=True, actor=user)
        except LedgerError as error:
            raise InvoiceError(str(error)) from error
        refresh_status(invoice)
    return payment


def issue_pending_invoices(limit=100):
    """Invoice paid orders that have none yet. Returns the count."""
    orders = Order.objects.filter(invoice__isnull=True, payment_transactions__kind="payment",
        payment_transactions__status="succeeded", payment_transactions__reconciled=True,
    ).select_related("store").distinct().order_by("created_at")[:limit]
    issued = 0
    for order in orders:
        try:
            issued += issue_invoice(order)[1]
        except (InvoiceError, IntegrityError):
            logger.exception("invoice issuing failed for order %s", order.pk)
    return issued


def month_bounds(year, month):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


@dataclass
class MonthlyTax:
    start: date
    end: date
    turnover: Decimal  # net of discounts and refunds, excluding PPN
    turnover_ytd: Decimal  # from 1 January up to and including this month
    pph_base: Decimal
    pph: Decimal
    pph_due: date
    vat: Decimal  # PPN keluaran collected from buyers
    vat_due: date
    invoice_count: int
    over_umkm_limit: bool
    input_vat: Decimal = Decimal(0)  # PPN masukan on recorded expenses

    @property
    def vat_payable(self):
        """PPN kurang bayar for the SPT Masa PPN (negative: lebih bayar, carried forward)."""
        return self.vat - self.input_vat


def _turnover(store, start, end):
    return sum((account.balance for account in account_balances(store, start=start, end=end)
        if account.type == "revenue"), Decimal(0))


def monthly_tax(store, year, month, profile=None):
    """What the seller owes for one month, from the ledger: turnover is booked on the day the
    money was received, which is when PPh Final and PPN arise for a retail seller."""
    profile = profile or tax_profile(store)
    start, end = month_bounds(year, month)
    turnover = _turnover(store, start, end)
    before = _turnover(store, date(year, 1, 1), start - timedelta(days=1)) if month > 1 else 0
    pph_base = turnover if profile.pph_regime == "umkm_final" else Decimal(0)
    if pph_base and profile.taxpayer_type == "individual":
        exemption_left = max(Decimal(0), INDIVIDUAL_EXEMPTION - before)
        pph_base = max(Decimal(0), turnover - exemption_left)
    balances = {account.code: account.balance
        for account in account_balances(store, start=start, end=end)}
    vat, input_vat = balances.get(VAT, Decimal(0)), balances.get(INPUT_VAT, Decimal(0))
    next_month_end = month_bounds(end.year + (end.month == 12), end.month % 12 + 1)[1]
    return MonthlyTax(start=start, end=end, turnover=turnover, turnover_ytd=before + turnover,
        pph_base=pph_base, pph=money(max(Decimal(0), pph_base) * UMKM_RATE),
        pph_due=next_month_end.replace(day=15), vat=vat, vat_due=next_month_end,
        invoice_count=Invoice.objects.filter(store=store, issued_on__range=(start, end)).count(),
        over_umkm_limit=before + turnover > UMKM_TURNOVER_LIMIT, input_vat=input_vat)
