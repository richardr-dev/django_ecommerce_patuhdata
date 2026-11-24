"""Lot (batch) and expiry tracking for products that switch it on (``Product.tracks_lots``).

Lots are store-wide per product or variant: they say *which batch* the units are, not where they are, so a
transfer between warehouses and goods sent to or back from a mitra leave the lots alone. Every other quantity
change follows the lots, hooked into ``inventory.record_movement`` (the one place stock changes):

- units coming in with a lot named (a goods receipt line: ``with receiving(code, expiry):``) join that lot,
  opening it when new;
- units coming in without one (a customer return, a count that found more, a cancelled surat jalan) go back to
  the lot most recently drawn, else to the lot ``TANPA-LOT``;
- units leaving draw the lot that expires first (FEFO among lots still good, then undated lots, expired lots last), then the next.

``StockLotMove`` rows tie each draw to its movement, so a lot traces to the invoices and surat jalan it left on
(a recall). Switching tracking on puts the units already held in a lot ``AWAL`` without an expiry date.
``controls.lots_tie`` warns when a product's lots do not add up to what is held."""
import contextvars
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import StockLot, StockLotMove

UNTRACKED_KINDS = {"reserve", "release", "transfer_in", "transfer_out", "consign_out", "consign_in", "deficit"}
NO_LOT, OPENING = "TANPA-LOT", "AWAL"
_incoming = contextvars.ContextVar("incoming_lot", default=None)


class LotError(ValueError):
    pass


@contextmanager
def receiving(code, expiry_date=None):
    """Units put on the shelf inside this block join lot ``code`` (expiring ``expiry_date``)."""
    token = _incoming.set(((code or "").strip()[:60], expiry_date))
    try:
        yield
    finally:
        _incoming.reset(token)


def _lots(product, variant):
    return StockLot.objects.select_for_update().filter(product=product, variant=variant)


def _lot(store, product, variant, code, expiry_date=None, today=None):
    lot = StockLot.objects.select_for_update().filter(product=product, variant=variant, code=code).first()
    if lot is None:
        lot = StockLot.objects.create(store=store, product=product, variant=variant, code=code,
            expiry_date=expiry_date, received_on=today or timezone.localdate())
    elif expiry_date and not lot.expiry_date:
        lot.expiry_date = expiry_date
        lot.save(update_fields=["expiry_date"])
    return lot


def follow(movement, product, variant, delta):
    """Move ``delta`` units of the lots of ``product``/``variant`` with ``movement`` (see the module notes)."""
    if not delta or not product.tracks_lots or movement.kind in UNTRACKED_KINDS:
        return
    store = movement.store
    if delta > 0:
        named = _incoming.get()
        if named and named[0]:
            lot = _lot(store, product, variant, named[0], named[1])
            lot.received_quantity += delta
        else:
            last = StockLotMove.objects.filter(lot__product=product, lot__variant=variant, quantity__lt=0
                ).select_related("lot").order_by("-id").first()
            lot = _lot(store, product, variant, last.lot.code if last else NO_LOT)
        lot.quantity += delta
        lot.save(update_fields=["quantity", "received_quantity"])
        StockLotMove.objects.create(lot=lot, movement=movement, quantity=delta)
        return
    _draw(product, variant, -delta, movement=movement)


def _draw(product, variant, units, *, movement=None, note=""):
    """Take ``units`` off the lots, soonest good expiry first (see the module notes)."""
    left = units
    today = timezone.localdate()
    held = _lots(product, variant).filter(quantity__gt=0)
    # Soonest expiry first among lots still good, then undated ones; an expired lot only when nothing else is left
    # (it should leave through Penyesuaian stok "rusak", and stays on the expiry warning until then).
    good = list(held.filter(expiry_date__gte=today).order_by("expiry_date", "received_on", "id"))
    undated = list(held.filter(expiry_date__isnull=True).order_by("received_on", "id"))
    expired = list(held.filter(expiry_date__lt=today).order_by("expiry_date", "received_on", "id"))
    for lot in good + undated + expired:
        take = min(left, lot.quantity)
        lot.quantity -= take
        lot.save(update_fields=["quantity"])
        StockLotMove.objects.create(lot=lot, movement=movement, note=note[:200], quantity=-take)
        left -= take
        if not left:
            return
    # More left than the lots hold (units that were never lotted): the lots stay short and ``lots_tie`` says so.


def draw(product, variant, units, note="Hilang dalam kiriman antar gudang"):
    """Units that left without a shelf movement (lost in transit) leave their lots too."""
    if units > 0 and product.tracks_lots:
        _draw(product, variant, units, note=note)


def held(product, variant=None):
    """Units a lot can be in: on a shelf, at a mitra, or on the road between warehouses."""
    from .stock import on_shelf, with_mitra
    from .stock_requests import in_transit
    return on_shelf(product, variant) + with_mitra(product, variant) + in_transit(product, variant)


@transaction.atomic
def start_tracking(product, *, actor=None):
    """Switch lots on for a product: what is held now becomes lot AWAL (per variant), without an expiry date."""
    from apps.catalog.models import Product
    product = Product.objects.select_for_update().get(pk=product.pk)
    if product.tracks_lots:
        return product
    product.tracks_lots = True
    product.save(update_fields=["tracks_lots"])
    items = list(product.variants.all()) or [None]
    for variant in items:
        units = held(product, variant)
        tracked = StockLot.objects.filter(product=product, variant=variant).aggregate(n=Sum("quantity", default=0))["n"]
        if units > tracked:
            lot = _lot(product.store, product, variant, OPENING)
            lot.quantity += units - tracked
            lot.received_quantity += units - tracked
            lot.save(update_fields=["quantity", "received_quantity"])
    return product


def stop_tracking(product):
    product.tracks_lots = False
    product.save(update_fields=["tracks_lots"])
    return product


@transaction.atomic
def set_expiry(lot, expiry_date):
    lot.expiry_date = expiry_date
    lot.save(update_fields=["expiry_date"])
    return lot


def expiring(store, *, within_days=30, today=None):
    """Lots still held that are past their date (``expired``) or reach it within ``within_days`` (``soon``)."""
    today = today or timezone.localdate()
    held_lots = StockLot.objects.filter(store=store, quantity__gt=0, expiry_date__isnull=False,
        product__tracks_lots=True).select_related("product", "variant")
    return {"expired": list(held_lots.filter(expiry_date__lt=today)),
        "soon": list(held_lots.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=within_days)))}


def mismatches(store):
    """``[(product, variant, lots, held)]`` for tracked items whose lots do not add up to the units held."""
    from apps.catalog.models import Product
    found = []
    for product in Product.objects.filter(store=store, tracks_lots=True).prefetch_related("variants"):
        for variant in list(product.variants.all()) or [None]:
            lotted = StockLot.objects.filter(product=product, variant=variant).aggregate(
                n=Sum("quantity", default=0))["n"]
            units = held(product, variant)
            if lotted != units:
                found.append((product, variant, lotted, units))
    return found


def trace(lot):
    """The movements a lot came in and left on, oldest first, with the document each belongs to."""
    from apps.accounting.inventory_reports import document_url
    rows = []
    for move in lot.moves.select_related("movement").order_by("id"):
        movement = move.movement
        rows.append({"move": move, "movement": movement, "url": document_url(movement) if movement else ""})
    return rows
