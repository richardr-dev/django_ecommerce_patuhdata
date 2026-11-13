"""Stok opname as a document (see ``models.StockCount``).

open → enter counted quantities → a *different* person approves → stock moves and the value of the
difference is booked:
    shortage  Selisih persediaan (6960) / Persediaan (1400)   at the item's cost price
    surplus   Persediaan / Selisih persediaan
Nothing is guessed: an item with a difference and no cost price blocks the approval. The maker may not
approve their own count when anyone else could (one-person stores are never locked out)."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import InventoryLevel

from .ledger import INVENTORY, STOCK_DIFFERENCE, LedgerError, post_entry
from .models import StockCount, StockCountLine


class CountError(ValueError):
    pass


def next_number(store, count_date):
    prefix = f"SO/{count_date:%Y}/"
    last = StockCount.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('/', 1)[1]) + 1) if last else 1:04d}"


@transaction.atomic
def open_count(store, *, warehouse, count_date, notes="", actor=None):
    """Start counting a warehouse: one line per product held there, with today's system quantity frozen."""
    if warehouse.store_id != store.pk:
        raise CountError("Gudang bukan milik toko ini.")
    if StockCount.objects.filter(store=store, warehouse=warehouse, status=StockCount.DRAFT).exists():
        raise CountError("Gudang ini masih punya stok opname yang belum selesai.")
    levels = list(InventoryLevel.objects.filter(warehouse=warehouse, product__store=store).select_related("product"))
    if not levels:
        raise CountError("Gudang ini belum menyimpan barang.")
    count = StockCount.objects.create(store=store, warehouse=warehouse, count_date=count_date, notes=notes[:300],
        number=next_number(store, count_date), created_by=actor)
    StockCountLine.objects.bulk_create(StockCountLine(count=count, product=level.product, system_qty=level.on_hand)
        for level in levels)
    return count


@transaction.atomic
def enter_counts(count, counted):
    """``counted`` maps a line id to the quantity found (None / missing leaves a line uncounted)."""
    count = StockCount.objects.select_for_update().get(pk=count.pk)
    if count.status != StockCount.DRAFT:
        raise CountError("Stok opname ini sudah selesai.")
    for line in count.lines.all():
        if line.pk in counted:
            value = counted[line.pk]
            if value is not None and value < 0:
                raise CountError(f"Hasil hitung {line.product.name} tidak boleh negatif.")
            line.counted_qty = value
            line.save(update_fields=["counted_qty"])


def differences(count):
    return [line for line in count.lines.select_related("product") if line.difference]


def summary(count):
    """``{"lines", "counted", "short_units", "extra_units", "value"}``; ``value`` is signed (negative = loss)."""
    rows = list(count.lines.select_related("product"))
    value = Decimal(0)
    for line in rows:
        if line.difference and line.product.cost_price is not None:
            value += line.difference * line.product.cost_price
    return {"lines": len(rows), "counted": sum(1 for r in rows if r.counted_qty is not None),
        "short_units": -sum(r.difference for r in rows if r.difference and r.difference < 0),
        "extra_units": sum(r.difference for r in rows if r.difference and r.difference > 0), "value": value}


@transaction.atomic
def approve(count, *, actor):
    from apps.orders.stock import StockError, stock_in, stock_out
    from apps.shops import permissions
    count = StockCount.objects.select_for_update().select_related("store", "warehouse").get(pk=count.pk)
    store = count.store
    if count.status != StockCount.DRAFT:
        raise CountError("Stok opname ini sudah selesai.")
    if not permissions.can(actor, store, "stock.count_approve"):
        raise CountError("Anda tidak berhak menyetujui stok opname.")
    if permissions.maker_may_not_check(store, actor, count.created_by, "stock.count_approve"):
        raise CountError("Stok opname yang Anda buat tidak boleh Anda setujui sendiri. Minta admin, manajer, atau "
            "keuangan lain.")
    lines = list(count.lines.select_related("product"))
    if any(line.counted_qty is None for line in lines):
        raise CountError("Masih ada barang yang belum dihitung. Isi semua, atau samakan dengan stok sistem.")
    value = Decimal(0)
    for line in lines:
        if not line.difference:
            continue
        if line.product.cost_price is None:
            raise CountError(f"{line.product.name} belum punya harga pokok. Isi dulu; selisihnya tidak dinilai asal-asalan.")
        line.unit_cost = line.product.cost_price
        line.save(update_fields=["unit_cost"])
        value += line.difference * line.unit_cost
    try:
        for line in lines:
            if line.difference:
                kwargs = dict(store=store, product=line.product, quantity=abs(line.difference), kind="opname",
                    reason=f"Stok opname {count.number}"[:200], source=("stock_count", str(count.pk)),
                    unit_cost=line.unit_cost, actor=actor)
                if line.difference > 0:
                    stock_in(warehouse=count.warehouse, **kwargs)
                else:
                    stock_out(**kwargs)
        if value:
            post_entry(store, date=count.count_date, strict=True, actor=actor, source=("stock_count", count.pk),
                memo=f"Selisih stok opname {count.number}", amounts={STOCK_DIFFERENCE: -value, INVENTORY: value})
    except (StockError, LedgerError) as error:
        raise CountError(str(error)) from error
    count.status, count.approved_by, count.approved_at = StockCount.APPROVED, actor, timezone.now()
    count.save(update_fields=["status", "approved_by", "approved_at"])
    return count


@transaction.atomic
def cancel(count):
    count = StockCount.objects.select_for_update().get(pk=count.pk)
    if count.status != StockCount.DRAFT:
        raise CountError("Stok opname ini sudah selesai.")
    count.status = StockCount.CANCELLED
    count.save(update_fields=["status"])
    return count
