"""Stok minus: selling more than the shelf holds, when the store allows it.

The shelf figure never goes below zero. A sale beyond it takes what is there and records the rest as
a ``StockDeficit`` at the cost known at that moment (provisional), with a ``deficit`` movement so the
whole sale stays traceable. The cost of goods sold for the full quantity is booked with the invoice
as always, so Persediaan in the books runs below the shelf value by exactly the open deficits.

The next goods receipt covers open deficits first (oldest first): those units leave the shelf again
with a ``deficit_cover`` movement, and the difference between the receipt's real cost and the
provisional one is posted to HPP against Persediaan. Only what is left of the receipt joins the
moving average. Cancelling or editing the sale releases its deficit.

Refused, never guessed: a product without a cost price, products with variants, and anyone who is not
allowed (``stock.oversell``: admin and manager; automatic jobs never sell short)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .inventory import record_movement
from .models import StockDeficit, StockDeficitCover

ZERO = Decimal(0)


def may_sell_short(store, actor):
    from apps.shops import permissions
    return bool(store.allow_negative_stock and actor is not None
        and permissions.can(actor, store, "stock.oversell"))


def open_deficits(store, product=None):
    rows = StockDeficit.objects.filter(store=store, released_at__isnull=True, quantity__gt=F("covered"))
    return rows.filter(product=product) if product is not None else rows


def summary(store):
    """``{"units", "value", "items"}`` of what is still open, valued at the provisional cost."""
    rows = list(open_deficits(store))
    return {"units": sum(r.open for r in rows), "items": len({r.product_id for r in rows}),
        "value": sum((r.open * r.provisional_cost for r in rows), ZERO)}


def minus_by_product(store):
    """Open shortfall per product id, for showing "minus N" next to its stock."""
    out = {}
    for row in open_deficits(store):
        out[row.product_id] = out.get(row.product_id, 0) + row.open
    return out


def sell_short(order_item, level, quantity, *, actor=None):
    """Record ``quantity`` units of ``order_item`` that the shelf could not give. ``level`` is the
    product's first (locked) warehouse level; it is not changed."""
    product = order_item.product
    if product.cost_price is None:
        raise ValueError(f"{product.name} belum punya harga pokok, jadi tidak bisa dijual melebihi stok.")
    deficit = StockDeficit.objects.create(store=order_item.order.store, product=product, order_item=order_item,
        inventory_level=level, quantity=quantity, provisional_cost=product.cost_price, created_by=actor)
    record_movement(store=deficit.store, order_item=order_item, inventory_level=level, kind="deficit",
        quantity=quantity, reason="Dijual melebihi stok (stok minus)", actor=actor,
        previous_on_hand=level.on_hand, new_on_hand=level.on_hand, previous_reserved=level.reserved,
        new_reserved=level.reserved, source_type="deficit", source_id=deficit.pk,
        unit_cost=deficit.provisional_cost)
    return deficit


@transaction.atomic
def cover(*, store, product, quantity, unit_cost, actor=None):
    """Use up to ``quantity`` just-received units to cover the product's open deficits. Returns how
    many units were used; the caller averages only the rest into the cost price."""
    from .stock import stock_out

    used = 0
    for deficit in open_deficits(store, product).select_for_update():
        matched = min(deficit.open, quantity - used)
        if matched <= 0:
            break
        stock_out(store=store, product=product, quantity=matched, kind="deficit_cover",
            reason=f"Menutup stok minus pesanan {deficit.order_item.order_id}"[:200],
            source=("deficit", deficit.pk), unit_cost=unit_cost, actor=actor)
        deficit.covered += matched
        deficit.save(update_fields=["covered"])
        covered = StockDeficitCover.objects.create(deficit=deficit, quantity=matched, unit_cost=unit_cost,
            covered_on=timezone.localdate())
        post_cover(covered, actor=actor)
        used += matched
    return used


def post_cover(covered, *, actor=None):
    """HPP was booked at the provisional cost; the receipt shows the real one. Post the difference."""
    from apps.accounting.ledger import COGS, INVENTORY, post_entry
    deficit = covered.deficit
    return post_entry(deficit.store, date=covered.covered_on, actor=actor, source=("stock_revalue", covered.pk),
        memo=f"Koreksi HPP stok minus {deficit.product.sku or deficit.product.name}"[:300],
        amounts={COGS: covered.adjustment, INVENTORY: -covered.adjustment})


@transaction.atomic
def release(order, *, actor=None, reason="pesanan_dibatalkan"):
    """The sale behind a deficit was cancelled or edited: what is still open simply ends, and units
    a receipt already gave to it go back on the shelf with their cost correction reversed."""
    from apps.accounting.ledger import reverse_source

    from .stock import stock_in
    for deficit in StockDeficit.objects.select_for_update().filter(order_item__order=order,
            released_at__isnull=True):
        if deficit.covered:
            stock_in(store=deficit.store, product=deficit.product, quantity=deficit.covered, kind="return",
                reason=reason[:200], source=("deficit", deficit.pk), warehouse=deficit.inventory_level.warehouse,
                actor=actor)
            for covered in deficit.covers.all():
                reverse_source(deficit.store, "stock_revalue", covered.pk, actor=actor)
        deficit.released_at = timezone.now()
        deficit.save(update_fields=["released_at"])
