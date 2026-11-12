"""Marketplace income reports: what each order really earned, and the payout that carries it.

An income report says, per order, how much the marketplace released and what it kept. Each row
is stored on its order (so reports show real net revenue and margin) and the rows released on
one day become one payout in the ledger: the marketplace receivable turns into money in the
bank, less the fees."""
import hashlib
import logging
from decimal import Decimal

from django.db import transaction

from apps.accounting.payouts import PayoutImportError, import_payout
from apps.orders.models import PaymentTransaction

from .models import ChannelOrder, ChannelOrderIncome

logger = logging.getLogger(__name__)
NOT_SALES = {"cancelled", "returned", "refunded"}


def _payout_reference(channel, released_on, order_ids):
    digest = hashlib.sha1(",".join(sorted(order_ids)).encode()).hexdigest()[:6]
    return f"{channel.platform}-{channel.id}-{released_on:%Y%m%d}-{digest}"


def _fee_breakdown(incomes):
    """The fees of a set of orders by ledger account."""
    from apps.accounting.ledger import FEE_ACCOUNTS
    split = {}
    for income in incomes:
        for name in ChannelOrderIncome.FEE_FIELDS:
            value = getattr(income, name) or Decimal(0)
            if value:
                code = FEE_ACCOUNTS[name]
                split[code] = split.get(code, Decimal(0)) + value
    return split


def import_income(channel, rows, *, default_release=None, actor=None, bank_account=None):
    """Store each row's fees on its order and book the payouts. Returns a summary with counts
    and the messages for rows that could not be used."""
    summary = {"orders": 0, "payouts": 0, "net": Decimal(0), "fees": Decimal(0),
        "already": 0, "errors": [], "warnings": []}
    ready = {}  # release date -> [(channel_order, payment, row, fee)]
    for row in rows:
        channel_order = ChannelOrder.objects.select_related("order").filter(channel=channel,
            external_order_id=row.order_id).first()
        if not channel_order:
            summary["errors"].append(f"{row.order_id}: pesanan belum diimpor. Impor pesanannya dulu.")
            continue
        order = channel_order.order
        if order.status in NOT_SALES:
            summary["warnings"].append(f"{row.order_id}: pesanan {order.get_status_display().lower()}, "
                "dilewati.")
            continue
        if hasattr(channel_order, "income") and channel_order.income.payout_reference:
            summary["already"] += 1
            continue
        payment = PaymentTransaction.objects.filter(order=order, provider=channel.platform,
            kind="payment", status="succeeded").first()
        if not payment:
            summary["errors"].append(f"{row.order_id}: pembayaran pesanan tidak ditemukan.")
            continue
        released_on = row.released_on or default_release
        if not released_on:
            summary["errors"].append(f"{row.order_id}: tanggal pencairan tidak ada di file. "
                "Isi tanggal pencairan saat mengunggah.")
            continue
        fee = payment.amount - row.net
        if fee < 0:
            summary["warnings"].append(f"{row.order_id}: dana cair Rp{row.net:,.0f} lebih besar "
                f"dari pembayaran Rp{payment.amount:,.0f}; selisih tidak dicatat sebagai biaya.")
            fee = Decimal(0)
        itemised = {name: value for name, value in row.fees.items() if name in
            ChannelOrderIncome.FEE_FIELDS}
        itemised["other_fee"] = itemised.get("other_fee", Decimal(0)) + max(
            Decimal(0), fee - sum(itemised.values(), Decimal(0)))
        income, _ = ChannelOrderIncome.objects.update_or_create(channel_order=channel_order,
            defaults={**{name: Decimal(0) for name in ChannelOrderIncome.FEE_FIELDS}, **itemised,
                "net_released": row.net, "released_on": released_on})
        ready.setdefault(released_on, []).append((income, payment, row, fee))
    for released_on, items in sorted(ready.items()):
        reference = _payout_reference(channel, released_on, [row.order_id for _, _, row, _ in items])
        data = {"paid_on": released_on, "lines": [{"number": row.line, "order": row.order_id,
            "amount": payment.amount, "fee": fee} for _, payment, row, fee in items]}
        try:
            with transaction.atomic():
                payout, created = import_payout(channel.store, channel.platform, reference, data,
                    actor=actor, bank_account=bank_account,
                    fee_breakdown=_fee_breakdown([income for income, *_ in items]))
                ChannelOrderIncome.objects.filter(pk__in=[income.pk for income, *_ in items]
                    ).update(payout_reference=reference)
        except PayoutImportError as error:
            summary["errors"].append(f"Pencairan {released_on:%d/%m/%Y}: {error}")
            continue
        summary["orders"] += len(items)
        if created:
            summary["payouts"] += 1
            summary["net"] += payout.net
            summary["fees"] += payout.fees
    return summary
