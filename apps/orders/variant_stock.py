"""Variant stock per warehouse.

``ProductVariant.stock`` is the store-wide total that sales, the storefront and every screen read. The
``VariantStock`` rows say where those units are. They are changed in one place only, ``apply`` (called by
``record_movement`` for every variant movement that changes the total), so the rows always add up to the total:

- units added go to the warehouse named, else the store's first active warehouse;
- units taken leave the warehouse named (refused if it holds too few), else the warehouses in priority order;
- a transfer is two movements (out of one, into the other) that leave the total where it was.

Variants stocked before this existed have no rows: ``heal`` puts any unlocated units in the first warehouse
before a change is applied, so old stock is never lost and never needs a migration. Reservations stay
store-wide on the variant."""
from django.db import transaction

from apps.catalog.models import VariantStock, Warehouse


class LocationShort(ValueError):
    pass


def _first_warehouse(store):
    return Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id").first() or \
        Warehouse.objects.filter(store=store).order_by("priority", "id").first()


def _rows(variant, *, lock=True):
    rows = VariantStock.objects.filter(variant=variant).select_related("warehouse").order_by(
        "warehouse__priority", "warehouse_id")
    return list(rows.select_for_update(of=("self",)) if lock else rows)


def heal(variant, total, store):
    """Make the rows add up to ``total`` (the variant's stock before the change being applied): unlocated units go
    to the first warehouse; rows holding more than the total give the excess back, last warehouse first."""
    rows = _rows(variant)
    held = sum(row.on_hand for row in rows)
    if held < total:
        warehouse = _first_warehouse(store)
        if warehouse is None:
            return rows
        row = next((r for r in rows if r.warehouse_id == warehouse.pk), None)
        if row is None:
            row = VariantStock.objects.create(variant=variant, warehouse=warehouse, on_hand=0)
            row.warehouse = warehouse
            rows.append(row)
        row.on_hand += total - held
        row.save(update_fields=["on_hand", "updated_at"])
    elif held > total:
        excess = held - total
        for row in reversed(rows):
            take = min(excess, row.on_hand)
            if take:
                row.on_hand -= take
                row.save(update_fields=["on_hand", "updated_at"])
                excess -= take
    return rows


@transaction.atomic
def apply(variant, *, store, before, delta, warehouse=None):
    """Spread a change of ``delta`` units in the variant's total (``before`` → ``before + delta``) over its
    locations. Returns ``[(warehouse, signed quantity)]``, one entry per location touched."""
    rows = heal(variant, before, store)
    if delta > 0:
        target = warehouse or _first_warehouse(store)
        if target is None:
            return [(None, delta)]
        row, _ = VariantStock.objects.select_for_update().get_or_create(variant=variant, warehouse=target)
        row.on_hand += delta
        row.save(update_fields=["on_hand", "updated_at"])
        return [(target, delta)]
    wanted = -delta
    if warehouse is not None:
        row = next((r for r in rows if r.warehouse_id == warehouse.pk), None)
        if row is None or row.on_hand < wanted:
            raise LocationShort(f"Stok {variant.sku or variant.name} di {warehouse.name} kurang: butuh {wanted}, "
                f"tersedia {row.on_hand if row else 0}.")
        rows = [row]
    parts = []
    for row in rows:
        take = min(wanted, row.on_hand)
        if not take:
            continue
        row.on_hand -= take
        row.save(update_fields=["on_hand", "updated_at"])
        parts.append((row.warehouse, -take))
        wanted -= take
        if not wanted:
            break
    if wanted:  # the total said the units were there; whatever no row held has no known location
        parts.append((None, -wanted))
    return parts


def at(variant, warehouse):
    """Units of ``variant`` in ``warehouse`` now (unlocated old stock counts as the first warehouse's)."""
    return dict(locations(variant)).get(warehouse.pk if warehouse else None, 0)


def locations(variant):
    """``[(warehouse id, units)]`` without changing anything: what ``heal`` would make of the rows."""
    rows = [(row.warehouse_id, row.on_hand) for row in _rows(variant, lock=False)]
    held = sum(units for _w, units in rows)
    if held < variant.stock:
        first = _first_warehouse(variant.product.store)
        if first is not None:
            found = dict(rows)
            found[first.pk] = found.get(first.pk, 0) + variant.stock - held
            rows = sorted(found.items(), key=lambda item: item[0] != first.pk)
    return rows


def drift(store):
    """Variants whose rows do not add up to their total: ``[(variant, total, rows)]`` (control and stock_audit)."""
    from django.db.models import Sum

    from apps.catalog.models import ProductVariant
    held = dict(VariantStock.objects.filter(variant__product__store=store).values("variant_id")
        .annotate(total=Sum("on_hand")).values_list("variant_id", "total"))
    return [(v, v.stock, held[v.pk]) for v in ProductVariant.objects.filter(product__store=store, pk__in=held)
        if held[v.pk] != v.stock]
