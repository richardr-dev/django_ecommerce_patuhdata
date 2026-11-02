import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounting.ledger import VAT, account_balances, money
from apps.orders.models import Order

from .models import Invoice, InvoiceSequence, TaxProfile

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


def _tax_rate(store, tax_base, tax):
    """The rate the order was charged: the store's current rate if it reproduces the tax,
    otherwise the rate implied by the amounts (the store changed its rate since)."""
    if not tax or not tax_base:
        return Decimal(0)
    if money(tax_base * store.tax_rate / 100) == tax:
        return store.tax_rate
    return (tax * 100 / tax_base).quantize(Decimal("0.01"))


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
            sequence, _ = InvoiceSequence.objects.get_or_create(store=store, year=issued_on.year)
            sequence = InvoiceSequence.objects.select_for_update().get(pk=sequence.pk)
            sequence.last_number += 1
            sequence.save(update_fields=["last_number"])
            invoice = Invoice.objects.create(store=store, order=order,
                number=f"{profile.invoice_prefix}/{issued_on.year}/{sequence.last_number:06d}",
                issued_on=issued_on, seller_name=profile.legal_name or store.name,
                seller_npwp=profile.npwp, seller_address=profile.address,
                seller_is_pkp=profile.is_pkp, buyer_name=order.customer_name,
                buyer_address=order.shipping_address, subtotal=order.subtotal,
                discount=order.discount, tax_base=tax_base,
                tax_rate=_tax_rate(store, tax_base, order.tax), tax=order.tax,
                shipping=order.shipping_cost,
                other_charges=order.total - tax_base - order.tax - order.shipping_cost,
                total=order.total)
    except IntegrityError:  # issued concurrently; the rollback also returned the number
        existing = Invoice.objects.filter(order=order).first()
        if existing:
            return existing, False
        raise
    return invoice, True


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
    vat: Decimal
    vat_due: date
    invoice_count: int
    over_umkm_limit: bool


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
    vat = next((account.balance for account in account_balances(store, start=start, end=end)
        if account.code == VAT), Decimal(0))
    next_month_end = month_bounds(end.year + (end.month == 12), end.month % 12 + 1)[1]
    return MonthlyTax(start=start, end=end, turnover=turnover, turnover_ytd=before + turnover,
        pph_base=pph_base, pph=money(max(Decimal(0), pph_base) * UMKM_RATE),
        pph_due=next_month_end.replace(day=15), vat=vat, vat_due=next_month_end,
        invoice_count=Invoice.objects.filter(store=store, issued_on__range=(start, end)).count(),
        over_umkm_limit=before + turnover > UMKM_TURNOVER_LIMIT)
