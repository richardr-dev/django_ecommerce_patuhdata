"""Pemetaan SKU marketplace ke katalog, dan paket (bundle).

A marketplace line is matched to the store's items in this order: the channel's own SKU map, a
bundle with that SKU, then the master SKU itself. A bundle line becomes one order line per
component so stock, cost and the ledger see the real goods; the price the buyer paid is shared
between the components in proportion to their list prices."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F

from apps.catalog.models import Bundle, BundleItem, ensure_store_sku_available
from apps.orders.models import OrderItem

from .adapters import OrderLine
from .models import ChannelSku, UnmappedSku

CENT = Decimal("0.01")


class MappingError(ValueError):
    pass


def expand_bundle(bundle, line):
    """One ``(OrderLine, product, variant)`` per component, sharing ``line``'s total price."""
    items = list(bundle.items.select_related("product", "variant"))
    if not items:
        raise MappingError(f"Paket {bundle.sku} belum punya isi.")
    total = line.line_total if line.line_total is not None else line.unit_price * line.quantity
    weights = [(item.unit_price or Decimal(0)) * item.quantity for item in items]
    if not sum(weights):
        weights = [Decimal(item.quantity) for item in items]
    result, allocated = [], Decimal(0)
    for index, (item, weight) in enumerate(zip(items, weights, strict=True)):
        quantity = item.quantity * line.quantity
        share = (total - allocated if index == len(items) - 1
            else (total * weight / sum(weights)).quantize(CENT, ROUND_HALF_UP))
        allocated += share
        target = item.variant or item.product
        result.append((OrderLine(sku=item.sku, quantity=quantity, bundle=bundle.sku,
            unit_price=(share / quantity).quantize(CENT, ROUND_HALF_UP), line_total=share,
            name=f"{target.name if item.variant_id else item.product.name}"), item.product,
            item.variant))
    return result


def find_mapping(channel, sku):
    """The channel's SKU map entry, or the bundle sold under that SKU, as ``(product, variant,
    bundle)`` (all None when neither exists)."""
    entry = ChannelSku.objects.select_related("product", "variant", "bundle").filter(
        channel=channel, external_sku=sku).first()
    if entry:
        return entry.product, entry.variant, entry.bundle
    bundle = Bundle.objects.filter(store=channel.store, sku=sku, is_active=True).first()
    return None, None, bundle


def note_unmapped(channel, line, placeholder=None):
    """Remember a SKU that matched nothing, once per SKU and name, counting how often."""
    row, created = UnmappedSku.objects.get_or_create(channel=channel, sku=line.sku[:120],
        name=(line.name or "")[:200], defaults={"store": channel.store, "placeholder": placeholder})
    if not created:
        UnmappedSku.objects.filter(pk=row.pk).update(times_seen=F("times_seen") + 1,
            resolved=False, **({"placeholder": placeholder} if placeholder else {}))
    return row


@transaction.atomic
def map_sku(channel, external_sku, *, product=None, variant=None, bundle=None):
    """Map a marketplace SKU to an item or a bundle and clear it from the review queue."""
    if bool(product) == bool(bundle):
        raise MappingError("Pilih produk atau paket.")
    entry, _ = ChannelSku.objects.update_or_create(channel=channel, external_sku=external_sku,
        defaults={"product": product, "variant": variant, "bundle": bundle})
    return entry


@transaction.atomic
def resolve_unmapped(row, *, product=None, variant=None, bundle=None):
    """Map a queued SKU and move the orders that used its placeholder onto the real item.
    Placeholder orders were imported without stock movement, so none is made here either."""
    entry = map_sku(row.channel, row.sku, product=product, variant=variant, bundle=bundle)
    moved = 0
    if row.placeholder_id and product and not bundle:
        moved = OrderItem.objects.filter(product=row.placeholder, sku=row.sku,
            order__store=row.store).update(product=product, variant=variant)
        if not OrderItem.objects.filter(product=row.placeholder).exists():
            row.placeholder.is_active = False
            row.placeholder.save(update_fields=["is_active"])
    UnmappedSku.objects.filter(channel=row.channel, sku=row.sku).update(resolved=True)
    return entry, moved


@transaction.atomic
def save_bundle(store, *, sku, name, components, bundle=None):
    """Create or update a bundle. ``components`` is ``[(product, variant, quantity)]``."""
    sku, name = sku.strip(), name.strip()
    if not sku or not name:
        raise MappingError("SKU dan nama paket wajib diisi.")
    if not components:
        raise MappingError("Paket harus berisi minimal satu barang.")
    from django.core.exceptions import ValidationError
    try:
        ensure_store_sku_available(store.id, sku)
    except ValidationError as error:
        raise MappingError(f"SKU {sku} sudah dipakai produk atau varian.") from error
    other = Bundle.objects.filter(store=store, sku=sku)
    if bundle:
        other = other.exclude(pk=bundle.pk)
    if other.exists():
        raise MappingError(f"SKU {sku} sudah dipakai paket lain.")
    if bundle:
        bundle.sku, bundle.name = sku, name
        bundle.save(update_fields=["sku", "name"])
        bundle.items.all().delete()
    else:
        bundle = Bundle.objects.create(store=store, sku=sku, name=name)
    BundleItem.objects.bulk_create(BundleItem(bundle=bundle, product=product, variant=variant,
        quantity=quantity) for product, variant, quantity in components)
    return bundle
