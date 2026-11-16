"""Konsinyasi: goods the mitra holds for the store.

Sending goods to a mitra is not a sale. The units leave the store's shelf but stay its property,
in "Persediaan di mitra", until the mitra sells them; the sale (revenue, receivable and cost of
goods) is booked when the settlement is invoiced. Units the mitra sends back return to the shelf."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import (
    CASH,
    COGS,
    CONSIGNED,
    INVENTORY,
    RECEIVABLE,
    SALES,
    SHIPPING,
    LedgerError,
    post_entry,
    register_backlog,
    unposted,
)
from apps.orders.stock import StockError, stock_in, stock_out

from .models import ConsignmentMovement, ConsignmentStock


class ConsignmentError(ValueError):
    pass


def _unit_cost(product, variant):
    cost = variant.cost_price if variant and variant.cost_price is not None else product.cost_price
    return cost or Decimal(0)


def _stock_row(partner, product, variant):
    row, _ = ConsignmentStock.objects.select_for_update().get_or_create(
        store=partner.store, partner=partner, product=product, variant=variant)
    return row


def _post_move(movement, *, outbound, strict=False, actor=None):
    """Move value between the shelf (Persediaan) and the mitra (Persediaan di mitra)."""
    value = abs(movement.quantity) * movement.unit_cost
    sign = 1 if outbound else -1
    return post_entry(movement.store, date=movement.date, strict=strict, actor=actor,
        source=("consignment_move", movement.pk),
        memo=f"{'Kirim ke' if outbound else 'Retur dari'} mitra {movement.stock.partner.name}: "
            f"{movement.stock.label} × {abs(movement.quantity)}",
        amounts={CONSIGNED: sign * value, INVENTORY: -sign * value})


@transaction.atomic
def send_to_partner(partner, *, product, variant=None, quantity, date=None, note="", actor=None):
    """Kirim barang ke mitra: off the shelf, into the mitra's stock, no revenue."""
    if quantity <= 0:
        raise ConsignmentError("Jumlah kirim harus lebih dari nol.")
    if variant and variant.product_id != product.id:
        raise ConsignmentError("Varian bukan milik produk ini.")
    if product.store_id != partner.store_id:
        raise ConsignmentError("Produk bukan milik perusahaan ini.")
    row = _stock_row(partner, product, variant)
    cost = _unit_cost(product, variant)
    try:
        stock_out(store=partner.store, product=product, variant=variant, quantity=quantity,
            kind="consign_out", reason=f"Kirim ke mitra {partner.name}",
            source=("consignment", partner.pk), unit_cost=cost, actor=actor)
    except StockError as error:
        raise ConsignmentError(str(error)) from error
    row.quantity += quantity
    row.save(update_fields=["quantity"])
    movement = ConsignmentMovement.objects.create(store=partner.store, stock=row, kind="send",
        quantity=quantity, unit_cost=cost, note=note[:200], actor=actor,
        date=date or timezone.localdate())
    try:
        _post_move(movement, outbound=True, strict=True, actor=actor)
    except LedgerError as error:
        raise ConsignmentError(str(error)) from error
    return movement


@transaction.atomic
def return_from_partner(partner, *, product, variant=None, quantity, date=None, note="",
        warehouse=None, actor=None):
    """Retur dari mitra: back from the mitra's stock to the shelf."""
    if quantity <= 0:
        raise ConsignmentError("Jumlah retur harus lebih dari nol.")
    row = _stock_row(partner, product, variant)
    if row.quantity < quantity:
        raise ConsignmentError(f"Stok di mitra hanya {row.quantity}, tidak bisa diretur {quantity}.")
    cost = _unit_cost(product, variant)
    stock_in(store=partner.store, product=product, variant=variant, quantity=quantity,
        kind="consign_in", reason=f"Retur dari mitra {partner.name}", warehouse=warehouse,
        source=("consignment", partner.pk), unit_cost=cost, actor=actor)
    row.quantity -= quantity
    row.save(update_fields=["quantity"])
    movement = ConsignmentMovement.objects.create(store=partner.store, stock=row, kind="return",
        quantity=-quantity, unit_cost=cost, note=note[:200], actor=actor,
        date=date or timezone.localdate())
    try:
        _post_move(movement, outbound=False, strict=True, actor=actor)
    except LedgerError as error:
        raise ConsignmentError(str(error)) from error
    return movement


# ── Settlement: the mitra's sales become the store's sales ───────────────────────────────────

def _settlement_items(settlement):
    for line in settlement.lines.select_related("order").all():
        if line.order_id:
            yield from line.order.items.select_related("product", "variant")


@transaction.atomic
def record_settlement_sales(settlement, *, actor=None):
    """Count what the settlement sold out of the mitra's stock and book the sale: the receivable,
    revenue and shipping, and the cost of the goods leaving Persediaan di mitra. Once per
    settlement."""
    if settlement.stock_movements.exists():
        return None
    cost_total = Decimal(0)
    today = timezone.localdate()
    for item in _settlement_items(settlement):
        row = _stock_row(settlement.partner, item.product, item.variant)
        cost = item.unit_cost if item.unit_cost is not None else _unit_cost(item.product,
            item.variant)
        row.quantity -= item.quantity
        row.save(update_fields=["quantity"])
        ConsignmentMovement.objects.create(store=settlement.store, stock=row, kind="sold",
            quantity=-item.quantity, unit_cost=cost, settlement=settlement, date=today,
            note=f"Settlement {settlement.invoice_number}", actor=actor)
        cost_total += cost * item.quantity
    return post_entry(settlement.store, date=today, actor=actor,
        source=("settlement", settlement.pk),
        memo=f"Penjualan mitra {settlement.partner.name} · {settlement.invoice_number}",
        amounts={RECEIVABLE: settlement.total_due, SALES: -settlement.subtotal,
            SHIPPING: -settlement.shipping_total, COGS: cost_total, CONSIGNED: -cost_total})


def post_settlement_payment(payment, *, actor=None):
    settlement = payment.settlement
    return post_entry(settlement.store, date=payment.payment_date, actor=actor,
        source=("settlement_payment", payment.pk),
        memo=f"Pembayaran mitra {settlement.partner.name} · {settlement.invoice_number}",
        amounts={payment.bank_account.account.code if payment.bank_account_id else CASH:
            payment.amount, RECEIVABLE: -payment.amount})


def stock_at_partners(store):
    """What every mitra holds, with its cost value."""
    rows = ConsignmentStock.objects.filter(store=store).exclude(quantity=0).select_related(
        "partner", "product", "variant")
    result = []
    for row in rows:
        cost = _unit_cost(row.product, row.variant)
        result.append({"row": row, "cost": cost, "value": cost * row.quantity})
    return result


@register_backlog
def post_unposted_settlements():
    """Settlements invoiced (and payments received) before the ledger booked them."""
    from .models import Settlement, SettlementPayment

    posted = 0
    invoiced = Settlement.objects.exclude(status__in=[Settlement.STATUS_DRAFT,
        Settlement.STATUS_SENT, Settlement.STATUS_DISPUTED, Settlement.STATUS_REVISED,
        Settlement.STATUS_CONFIRMED])
    for settlement in unposted(invoiced, "settlement"):
        posted += bool(record_settlement_sales(settlement))
    for payment in unposted(SettlementPayment.objects.all(), "settlement_payment"):
        posted += bool(post_settlement_payment(payment))
    return posted
