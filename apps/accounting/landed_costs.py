"""Biaya perolehan persediaan (landed cost): freight, import duty and handling that belong to the cost of goods.

The cost arrives as an ordinary supplier bill of category 1460 (the forwarder, the customs broker), so it is
paid, aged and withheld like any bill; it sits in 1460 "Biaya perolehan persediaan (belum dialokasikan)". An
allocation spreads the bill (net of creditable PPN) over the lines of one or more goods receipts, by value or by
quantity. For each line the share of the units still held (shelf and mitra, at most the units received) goes into
Persediaan and the item's moving-average cost; the share of units already sold goes to HPP. The entry is
``("landed_cost", id)``: Persediaan + HPP against 1460. Void reverses it and takes the cost back off, refused once
fewer units are held than took the share (they have left at the higher cost)."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from .ledger import (
    COGS,
    INVENTORY,
    LANDED,
    money,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from .models import Bill, GoodsReceipt, LandedCost, LandedCostLine

ZERO = Decimal(0)


class LandedCostError(ValueError):
    pass


def bill_amount(bill):
    """What a 1460 bill puts on the goods: its total less the PPN a PKP credits."""
    from .payables import _can_credit_input_vat
    vat = bill.tax if _can_credit_input_vat(bill.store) else ZERO
    return bill.total - vat


def unallocated(bill):
    done = LandedCost.objects.filter(bill=bill, status=LandedCost.POSTED).values_list("amount", flat=True)
    return bill_amount(bill) - sum(done, ZERO)


def open_bills(store):
    """Bills of category 1460 with something left to allocate."""
    return [bill for bill in Bill.objects.filter(store=store, category=LANDED).exclude(status=Bill.STATUS_VOID)
        .select_related("supplier").order_by("-bill_date") if unallocated(bill) > 0]


def _split(amount, weights):
    """``amount`` over ``weights`` to the cent, the remainder on the largest weight."""
    total = sum(weights, ZERO)
    if not total:
        raise LandedCostError("Penerimaan yang dipilih tidak punya nilai atau jumlah untuk dibagi.")
    parts = [(amount * w / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for w in weights]
    parts[max(range(len(parts)), key=lambda i: weights[i])] += amount - sum(parts, ZERO)
    return parts


def _held(product, variant):
    from apps.orders.stock import on_shelf, with_mitra
    return on_shelf(product, variant) + with_mitra(product, variant)


def _revalue(holder, held, value):
    """Add (or, negative, take off) ``value`` spread over the ``held`` units of a product or variant."""
    if held <= 0 or not value:
        return
    cost = Decimal(holder.cost_price or 0)
    holder.cost_price = money((cost * held + value) / held)
    holder.save(update_fields=["cost_price"])


def next_number(store, date):
    prefix = f"LC/{date:%Y}/"
    last = LandedCost.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('/', 1)[1]) + 1) if last else 1:04d}"


def post_landed_cost(cost, *, strict=False, actor=None):
    if not cost.amount:
        return None
    return post_entry(cost.store, date=cost.date, strict=strict, actor=actor, source=("landed_cost", cost.pk),
        memo=f"Biaya perolehan {cost.number} · tagihan {cost.bill.supplier.name} {cost.bill.number}".strip(),
        amounts={INVENTORY: cost.to_stock, COGS: cost.to_cogs, LANDED: -cost.amount})


@transaction.atomic
def allocate(store, *, bill, receipts, date, method=LandedCost.BY_VALUE, amount=None, notes="", actor=None):
    """Spread ``amount`` (default: all that is left of ``bill``) over the lines of ``receipts``."""
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.store_id != store.pk or bill.category != LANDED:
        raise LandedCostError("Pilih tagihan ongkos angkut / bea masuk (kategori 1460) perusahaan ini.")
    if bill.status == Bill.STATUS_VOID:
        raise LandedCostError("Tagihan ini sudah dibatalkan.")
    left = unallocated(bill)
    amount = money(amount if amount is not None else left)
    if amount <= 0:
        raise LandedCostError("Tagihan ini sudah dialokasikan seluruhnya.")
    if amount > left:
        raise LandedCostError(f"Sisa tagihan yang belum dialokasikan hanya {left:,.0f}.".replace(",", "."))
    if method not in dict(LandedCost.METHODS):
        raise LandedCostError("Cara pembagian tidak dikenal.")
    receipts = list(GoodsReceipt.objects.filter(store=store, pk__in=[r.pk for r in receipts]))
    if not receipts:
        raise LandedCostError("Pilih minimal satu penerimaan barang.")
    lines = [line for receipt in receipts for line in receipt.lines.select_related("product", "variant")]
    if not lines:
        raise LandedCostError("Penerimaan yang dipilih tidak berisi barang.")
    weights = [Decimal(line.quantity) * (line.unit_cost if method == LandedCost.BY_VALUE else 1) for line in lines]
    shares = _split(amount, weights)

    cost = LandedCost.objects.create(store=store, number=next_number(store, date), date=date, bill=bill,
        method=method, amount=amount, notes=notes or "", created_by=actor)
    cost.receipts.set(receipts)
    room = {}   # units of an item still held, shared out between the lines that bring it in
    added = {}
    for line, share in zip(lines, shares, strict=True):
        key = (line.product_id, line.variant_id)
        if key not in room:
            room[key] = _held(line.product, line.variant)
        held = max(0, min(room[key], line.quantity))
        room[key] -= held
        to_stock = money(share * held / line.quantity) if line.quantity else ZERO
        LandedCostLine.objects.create(landed_cost=cost, receipt_line=line, product=line.product, variant=line.variant,
            quantity=line.quantity, held=held, share=share, to_stock=to_stock, to_cogs=share - to_stock)
        holder, total = added.get(key, (line.variant or line.product, ZERO))
        added[key] = (holder, total + to_stock)
    for (product_id, variant_id), (holder, value) in added.items():
        product = holder.product if variant_id else holder
        _revalue(holder, _held(product, holder if variant_id else None), value)
    cost.to_stock = sum((line.to_stock for line in cost.lines.all()), ZERO)
    cost.to_cogs = amount - cost.to_stock
    cost.save(update_fields=["to_stock", "to_cogs"])
    post_landed_cost(cost, strict=True, actor=actor)
    return cost


@transaction.atomic
def void(cost, *, actor=None):
    cost = LandedCost.objects.select_for_update().get(pk=cost.pk)
    if cost.status == LandedCost.VOID:
        return cost
    taken = {}
    for line in cost.lines.select_related("product", "variant"):
        key = (line.product_id, line.variant_id)
        holder, held, value = taken.get(key, (line.variant or line.product, 0, ZERO))
        taken[key] = (holder, held + line.held, value + line.to_stock)
    for (_p, variant_id), (holder, held, value) in taken.items():
        product = holder.product if variant_id else holder
        now = _held(product, holder if variant_id else None)
        if now < held:
            raise LandedCostError(f"{holder}: sebagian barang yang menanggung biaya ini sudah keluar; koreksi lewat "
                "jurnal penyesuaian, bukan pembatalan.")
        _revalue(holder, now, -value)
    reverse_source(cost.store, "landed_cost", cost.pk, actor=actor, strict=True, memo=f"Pembatalan {cost.number}")
    cost.status = LandedCost.VOID
    cost.save(update_fields=["status"])
    return cost


def waiting(store):
    """What 1460 should hold: every live 1460 bill's amount not yet allocated."""
    return sum((unallocated(bill) for bill in Bill.objects.filter(store=store, category=LANDED)
        .exclude(status=Bill.STATUS_VOID)), ZERO)


@register_backlog
def post_unposted_landed_costs():
    posted = 0
    for cost in unposted(LandedCost.objects.filter(status=LandedCost.POSTED), "landed_cost"):
        posted += bool(post_landed_cost(cost))
    return posted
