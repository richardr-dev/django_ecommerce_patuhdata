import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.channels.adapters import PLATFORMS
from apps.orders.models import PaymentTransaction, Refund

from .models import Account, JournalEntry, JournalLine

logger = logging.getLogger(__name__)

CASH, GATEWAY, MARKETPLACE, INVENTORY = "1100", "1200", "1300", "1400"
VAT, UNIDENTIFIED = "2100", "2200"
SALES, SHIPPING, REFUNDS, DISCOUNTS = "4100", "4200", "4800", "4900"
COGS, FEES = "5100", "6100"
# Every store gets this chart on its first posting. Refunds and discounts are contra-revenue:
# revenue accounts whose balance runs on the debit side and so reduce net sales.
CHART = [
    (CASH, "Kas & bank", "asset"),
    (GATEWAY, "Piutang payment gateway", "asset"),
    (MARKETPLACE, "Piutang marketplace", "asset"),
    (INVENTORY, "Persediaan barang", "asset"),
    (VAT, "Utang PPN keluaran", "liability"),
    (UNIDENTIFIED, "Pembayaran belum teridentifikasi", "liability"),
    (SALES, "Penjualan", "revenue"),
    (SHIPPING, "Pendapatan ongkos kirim", "revenue"),
    (REFUNDS, "Retur & refund penjualan", "revenue"),
    (DISCOUNTS, "Potongan penjualan", "revenue"),
    (COGS, "Harga pokok penjualan", "expense"),
    (FEES, "Biaya payment gateway & marketplace", "expense"),
]


class LedgerError(ValueError):
    pass


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def chart_of_accounts(store):
    accounts = {account.code: account for account in store.ledger_accounts.all()}
    for code, name, kind in CHART:
        if code not in accounts:
            accounts[code], _ = Account.objects.get_or_create(store=store, code=code,
                defaults={"name": name, "type": kind})
    return accounts


def _post(store, *, date, memo, amounts, **source):
    """Post signed amounts by account code: positive is a debit, negative a credit."""
    amounts = {code: money(value) for code, value in amounts.items() if money(value)}
    if sum(amounts.values()) != 0:
        raise LedgerError(f"Unbalanced entry: {memo}")
    accounts = chart_of_accounts(store)
    entry = JournalEntry.objects.create(store=store, date=date, memo=memo[:300], **source)
    JournalLine.objects.bulk_create(JournalLine(entry=entry, account=accounts[code],
        debit=max(value, 0), credit=max(-value, 0)) for code, value in amounts.items())
    return entry


def receivable_account(provider):
    """Where money waits after a sale: the seller's bank for a transfer, otherwise with the
    gateway or marketplace until its payout."""
    return (CASH if provider == "bank_transfer" else
        MARKETPLACE if provider in PLATFORMS else GATEWAY)


def cost_of_goods(order):
    """Copy each item's current unit cost onto it once and return the order's total cost.
    Items whose product has no cost price are left out."""
    total = Decimal(0)
    for item in order.items.select_related("product", "variant"):
        if item.unit_cost is None:
            cost = item.variant.cost_price if item.variant else None
            item.unit_cost = cost if cost is not None else item.product.cost_price
            if item.unit_cost is None:
                continue
            item.save(update_fields=["unit_cost"])
        total += item.unit_cost * item.quantity
    return total


def post_payment(payment):
    order = payment.order
    date = timezone.localdate(payment.updated_at)
    received = receivable_account(payment.provider)
    if not payment.reconciled:
        # Money arrived for an order that could not take it (e.g. expired): owed back until
        # finance refunds or matches it.
        return _post(order.store, date=date, payment=payment,
            memo=f"Pembayaran belum teridentifikasi #{str(order.id)[:8]} · {payment.provider}",
            amounts={received: payment.amount, UNIDENTIFIED: -payment.amount})
    # Sales is the residual, so the entry balances even for orders priced by older rules.
    sales = order.total - order.shipping_cost - order.tax + order.discount
    cost = cost_of_goods(order)
    return _post(order.store, date=date, payment=payment,
        memo=f"Penjualan #{str(order.id)[:8]} · {payment.provider}",
        amounts={received: payment.amount, DISCOUNTS: order.discount, SALES: -sales,
            SHIPPING: -order.shipping_cost, VAT: -order.tax, COGS: cost, INVENTORY: -cost})


def post_refund(refund):
    order = refund.order
    # Reverse the PPN in proportion to the share of the order total refunded.
    vat = money(refund.amount * order.tax / order.total) if order.total else Decimal(0)
    paid_from = CASH if refund.method == "manual_transfer" else GATEWAY
    return _post(order.store, date=timezone.localdate(refund.updated_at), refund=refund,
        memo=f"Refund #{str(order.id)[:8]} · {refund.get_reason_display()}",
        amounts={REFUNDS: refund.amount - vat, VAT: vat, paid_from: -refund.amount})


def post_payout(payout):
    """The provider paid out: its receivable becomes money in the bank, less its fees."""
    return _post(payout.store, date=payout.paid_on, payout=payout,
        memo=f"Pencairan {payout.provider} {payout.reference}",
        amounts={CASH: payout.net, FEES: payout.fees,
            receivable_account(payout.provider): -payout.gross})


def post_pending_entries(limit=100):
    """Post succeeded payments and refunds that have no journal entry yet. Returns the count."""
    posted = 0
    payments = PaymentTransaction.objects.filter(kind="payment", status="succeeded",
        journal_entry__isnull=True).select_related("order__store").order_by("id")[:limit]
    refunds = Refund.objects.filter(status="succeeded", journal_entry__isnull=True).select_related(
        "order__store").order_by("created_at")[:limit]
    for post, source in [(post_payment, item) for item in payments] + [
            (post_refund, item) for item in refunds]:
        try:
            with transaction.atomic():
                post(source)
            posted += 1
        except IntegrityError:  # posted concurrently by another worker
            continue
        except LedgerError:  # leave it unposted for finance without blocking the rest
            logger.exception("ledger posting failed for %s %s", type(source).__name__, source.pk)
    return posted


def account_balances(store, *, start=None, end=None):
    """Debit and credit totals per account, with the balance on each account's normal side."""
    period = Q()
    if start:
        period &= Q(lines__entry__date__gte=start)
    if end:
        period &= Q(lines__entry__date__lte=end)
    accounts = Account.objects.filter(store=store).annotate(
        total_debit=Sum("lines__debit", filter=period, default=0),
        total_credit=Sum("lines__credit", filter=period, default=0))
    for account in accounts:
        debit_normal = account.type in {"asset", "expense"}
        account.balance = (account.total_debit - account.total_credit if debit_normal
            else account.total_credit - account.total_debit)
    return list(accounts)
