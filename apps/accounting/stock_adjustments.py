"""Penyesuaian stok: the one way to change stock that no purchase, sale, transfer or count explains.

Standard inventory flow: every quantity on the shelf comes from a document, and every document that moves
stock moves Persediaan with it.

    Barang masuk     goods receipt / bill "Terima barang" / Beli tunai   Persediaan / GRNI or Utang usaha
    Barang keluar    invoice, surat jalan, POS                           HPP / Persediaan
    Pindah gudang    stock request, transfer                             no entry (same company, same cost)
    Stok opname      count, approved by a second person                  Selisih persediaan ↔ Persediaan
    Penyesuaian      this module: a stated reason, valued at cost        reason's account ↔ Persediaan

Each reason has one direction and one account (``REASONS``): damaged goods go to 6965, lost goods to Selisih
persediaan 6960, samples to 6250, goods the owner takes to Prive 3200, stock that was never recorded to Modal
pemilik 3100. Units leave at the item's moving-average cost (a variant's from the document's warehouse); units added come in at the cost typed (or the
current cost) and join the moving average. Nothing is valued at a guess: an item without a cost price is
refused. Above the store's threshold a penyesuaian waits for approval by someone else (kind
``stock_adjustment``).

Before the books start (no journal entry yet) Kelola stok and the product form may still set quantities
directly: that is the stock count the Saldo awal books in one entry. From the first entry on, every
quantity typed there becomes a penyesuaian (``set_quantity``)."""
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import InventoryLevel, ProductVariant

from .ledger import (
    DAMAGED_STOCK,
    DRAWINGS,
    EQUITY,
    INVENTORY,
    SAMPLES,
    STOCK_DIFFERENCE,
    LedgerError,
    live_entry,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from .models import JournalEntry, StockAdjustment, StockAdjustmentLine

SOURCE = "stock_adjustment"
CENT = Decimal("0.01")


class AdjustmentError(ValueError):
    pass


@dataclass(frozen=True)
class Reason:
    label: str
    direction: str  # "out", "in" or "both"
    account: str
    hint: str


REASONS = {
    "damaged": Reason("Barang rusak / kedaluwarsa", "out", DAMAGED_STOCK,
        "Tidak bisa dijual lagi. Nilainya menjadi beban persediaan rusak."),
    "lost": Reason("Hilang / susut", "out", STOCK_DIFFERENCE,
        "Barang tidak ditemukan atau berkurang di luar penjualan."),
    "sample": Reason("Sampel & promosi", "out", SAMPLES,
        "Diberikan gratis untuk promosi, endorse, atau contoh barang."),
    "owner": Reason("Diambil pemilik", "out", DRAWINGS,
        "Dipakai pribadi oleh pemilik; dicatat sebagai prive, bukan beban usaha."),
    "found": Reason("Ditemukan / lebih", "in", STOCK_DIFFERENCE,
        "Barang yang sempat dianggap hilang atau lebih dari catatan."),
    "opening": Reason("Stok lama belum tercatat", "in", EQUITY,
        "Barang yang sudah dimiliki sebelum dicatat di sistem; menambah modal pemilik."),
    "correction": Reason("Koreksi salah catat", "both", STOCK_DIFFERENCE,
        "Angka stok yang salah ketik atau salah hitung."),
}


def books_started(store):
    """From the first journal entry on, a quantity change must be a document with a journal."""
    return JournalEntry.objects.filter(store=store).exists()


def next_number(store, day):
    prefix = f"PS/{day:%Y}/"
    last = StockAdjustment.objects.filter(store=store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('/', 1)[1]) + 1) if last else 1:04d}"


def current_cost(product, variant=None):
    if variant is not None and variant.cost_price is not None:
        return variant.cost_price
    return product.cost_price


def _held(product, variant):
    from apps.orders.stock import on_shelf, with_mitra
    return on_shelf(product, variant) + with_mitra(product, variant)


def _clean_lines(store, warehouse, reason, lines):
    """``lines``: ``[{"product", "variant", "quantity" (signed), "unit_cost" (optional, additions only)}]``."""
    if not lines:
        raise AdjustmentError("Isi minimal satu barang.")
    cleaned = []
    for raw in lines:
        product, variant = raw["product"], raw.get("variant")
        quantity = int(raw["quantity"])
        name = f"{product.name} · {variant.name}" if variant else product.name
        if product.store_id != store.pk or (variant is not None and variant.product_id != product.pk):
            raise AdjustmentError("Barang tidak ditemukan.")
        if variant is None and product.variants.filter(is_active=True).exists():
            raise AdjustmentError(f"{product.name} punya varian. Pilih variannya.")
        if not quantity:
            raise AdjustmentError(f"Jumlah {name} tidak boleh nol.")
        if reason.direction == "out" and quantity > 0 or reason.direction == "in" and quantity < 0:
            word = "mengurangi" if reason.direction == "out" else "menambah"
            raise AdjustmentError(f"Alasan \"{reason.label}\" hanya untuk {word} stok ({name}).")
        cost = current_cost(product, variant)
        if quantity > 0 and raw.get("unit_cost") not in (None, ""):
            cost = Decimal(raw["unit_cost"])
            if cost < 0:
                raise AdjustmentError(f"Harga pokok {name} tidak boleh negatif.")
        if cost is None:
            raise AdjustmentError(f"{name} belum punya harga pokok. Isi harga pokoknya dulu; stok tidak dinilai "
                "asal-asalan.")
        cost = Decimal(cost).quantize(CENT)
        cleaned.append({"product": product, "variant": variant, "quantity": quantity, "unit_cost": cost,
            "value": (quantity * cost).quantize(CENT), "name": name, "anywhere": bool(raw.get("anywhere"))})
    return cleaned


def post_adjustment(store, *, warehouse, adjusted_on, reason, lines, notes="", actor=None, approved=False):
    """Move the stock and book its value. Returns the ``StockAdjustment``, or the ``ApprovalRequest`` when
    the value is above the store's approval threshold and someone else must approve it first."""
    from apps.orders.stock import check_warehouse
    from apps.shops.audit import log as audit_log

    from . import approvals
    if reason not in REASONS:
        raise AdjustmentError("Pilih alasan penyesuaian.")
    rule = REASONS[reason]
    try:
        check_warehouse(store, warehouse)
    except ValueError as error:
        raise AdjustmentError(str(error)) from error
    if warehouse is None:
        raise AdjustmentError("Pilih gudang.")
    cleaned = _clean_lines(store, warehouse, rule, lines)
    value = sum((line["value"] for line in cleaned), Decimal(0))
    if not approved and approvals.required(store, "stock_adjustment", abs(value), actor):
        payload = {"warehouse_id": warehouse.pk, "adjusted_on": adjusted_on.isoformat(), "reason": reason,
            "notes": notes[:300], "lines": [[line["product"].pk, line["variant"].pk if line["variant"] else None,
            line["quantity"], str(line["unit_cost"]), line["anywhere"]] for line in cleaned]}
        summary = f"Penyesuaian stok {rule.label}: {', '.join(line['name'] for line in cleaned)}"
        return approvals.submit(store, "stock_adjustment", abs(value), summary, payload, actor)
    try:
        with transaction.atomic():
            adjustment = _post(store, warehouse, adjusted_on, reason, rule, cleaned, value, notes, actor)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    audit_log(store, actor, "stock_adjustment.posted", adjustment, number=adjustment.number, reason=rule.label,
        value=value)
    return adjustment


def _post(store, warehouse, adjusted_on, reason, rule, cleaned, value, notes, actor):
    from apps.orders.stock import StockError, moving_average, stock_in, stock_out
    adjustment = StockAdjustment.objects.create(store=store, warehouse=warehouse, adjusted_on=adjusted_on,
        reason=reason, account_code=rule.account, notes=notes[:300], value=value, created_by=actor,
        number=next_number(store, adjusted_on))
    memo = f"Penyesuaian stok {adjustment.number}: {rule.label}"
    source = (SOURCE, str(adjustment.pk))
    try:
        for line in cleaned:
            product, variant, quantity = line["product"], line["variant"], line["quantity"]
            StockAdjustmentLine.objects.create(adjustment=adjustment, product=product, variant=variant,
                quantity=quantity, unit_cost=line["unit_cost"], value=line["value"])
            kwargs = {"store": store, "product": product, "variant": variant, "quantity": abs(quantity), "kind": "adjust",
                "reason": memo[:200], "source": source, "unit_cost": line["unit_cost"], "actor": actor}
            if quantity < 0:
                # a variant's store-wide total typed on the product page leaves from wherever it is held
                stock_out(warehouse=None if line["anywhere"] else warehouse, **kwargs)
                continue
            holder = ProductVariant.objects.select_for_update().get(pk=variant.pk) if variant else product
            held = _held(product, variant)
            stock_in(warehouse=warehouse, **kwargs)
            if holder.cost_price != line["unit_cost"]:
                holder.cost_price = moving_average(max(held, 0), holder.cost_price, quantity, line["unit_cost"])
                holder.save(update_fields=["cost_price"])
    except StockError as error:
        raise AdjustmentError(str(error)) from error
    _book(adjustment, strict=True, actor=actor)
    return adjustment


def _book(adjustment, *, strict, actor=None):
    if not adjustment.value:
        return None
    return post_entry(adjustment.store, date=adjustment.adjusted_on, strict=strict, actor=actor,
        source=(SOURCE, adjustment.pk), memo=f"Penyesuaian stok {adjustment.number}: {adjustment.reason_label}",
        amounts={INVENTORY: adjustment.value, adjustment.account_code: -adjustment.value})


@transaction.atomic
def book_moved(store, *, warehouse, reason, lines, notes="", actor=None, adjusted_on=None):
    """A bulk change (the product CSV import) that already moved the stock with its own movements: record the
    document and book its value, without moving the stock again. The caller points its movements at it."""
    rule = REASONS[reason]
    cleaned = _clean_lines(store, warehouse, rule, lines)
    value = sum((line["value"] for line in cleaned), Decimal(0))
    adjusted_on = adjusted_on or timezone.localdate()
    adjustment = StockAdjustment.objects.create(store=store, warehouse=warehouse, adjusted_on=adjusted_on,
        reason=reason, account_code=rule.account, notes=notes[:300], value=value, created_by=actor,
        number=next_number(store, adjusted_on))
    StockAdjustmentLine.objects.bulk_create(StockAdjustmentLine(adjustment=adjustment, product=line["product"],
        variant=None, quantity=line["quantity"], unit_cost=line["unit_cost"], value=line["value"]) for line in cleaned)
    try:
        _book(adjustment, strict=False, actor=actor)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    return adjustment


@transaction.atomic
def void_adjustment(adjustment, *, actor=None):
    """Put the stock back the way it was and reverse the entry. The cost price an addition changed is kept."""
    from apps.orders.stock import StockError, stock_in, stock_out
    from apps.shops.audit import log as audit_log
    adjustment = StockAdjustment.objects.select_for_update().select_related("store", "warehouse").get(pk=adjustment.pk)
    if adjustment.status == StockAdjustment.VOID:
        raise AdjustmentError("Penyesuaian ini sudah dibatalkan.")
    store, source = adjustment.store, (SOURCE, str(adjustment.pk))
    memo = f"Batal penyesuaian stok {adjustment.number}"
    try:
        for line in adjustment.lines.select_related("product", "variant"):
            kwargs = {"store": store, "product": line.product, "variant": line.variant, "quantity": abs(line.quantity),
                "kind": "adjust", "reason": memo, "source": source, "unit_cost": line.unit_cost, "actor": actor}
            if line.quantity > 0:
                stock_out(warehouse=adjustment.warehouse, **kwargs)
            else:
                stock_in(warehouse=adjustment.warehouse, **kwargs)
        reverse_source(store, SOURCE, adjustment.pk, strict=True, actor=actor, memo=memo)
    except StockError as error:
        raise AdjustmentError(f"{error} Barangnya sudah keluar lagi, jadi penyesuaian tidak bisa dibatalkan.") from error
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    adjustment.status, adjustment.voided_by, adjustment.voided_at = StockAdjustment.VOID, actor, timezone.now()
    adjustment.save(update_fields=["status", "voided_by", "voided_at"])
    audit_log(store, actor, "stock_adjustment.voided", adjustment, number=adjustment.number)
    return adjustment


def set_quantity(store, *, new_quantity, level=None, variant=None, warehouse=None, reason="correction", notes="",
        actor=None, adjusted_on=None):
    """Kelola stok and the product form: the person typed the quantity on the shelf. Before the books start the
    figure is set with a movement (Saldo awal books it later); afterwards the difference is a penyesuaian with
    ``reason``. For a variant, ``warehouse`` makes ``new_quantity`` that location's figure (without it, the
    store-wide total). Returns the adjustment, an ``ApprovalRequest``, or None (nothing booked)."""
    from apps.orders.inventory import record_movement
    from apps.orders.stock import default_warehouse
    from apps.orders.variant_stock import at
    if new_quantity < 0:
        raise AdjustmentError("Stok harus berupa angka nol atau lebih.")
    target = level or variant
    reserved = target.reserved
    if level:
        current = level.on_hand
        warehouse = level.warehouse
    elif warehouse is not None:
        current = at(variant, warehouse)
        reserved = max(0, reserved - (variant.stock - current))  # what the other locations cannot cover
    else:
        current = variant.stock
    if new_quantity < reserved:
        raise AdjustmentError(f"Stok tidak boleh kurang dari {reserved} unit yang sedang direservasi.")
    if new_quantity == current:
        return None
    delta = new_quantity - current
    if not books_started(store):
        with transaction.atomic():
            locked = (InventoryLevel if level else ProductVariant).objects.select_for_update().get(pk=target.pk)
            before = locked.on_hand if level else locked.stock
            if level:
                locked.on_hand = before + delta
                locked.save(update_fields=["on_hand", "updated_at"])
            else:
                locked.stock = before + delta
                locked.save(update_fields=["stock"])
            record_movement(store=store, kind="adjust", quantity=abs(delta),
                reason=(notes or "Stok sebelum pembukuan dimulai")[:200], actor=actor,
                inventory_level=locked if level else None, variant=None if level else locked,
                previous_on_hand=before, new_on_hand=before + delta, previous_reserved=locked.reserved,
                new_reserved=locked.reserved, source_type="setup", warehouse=None if level else warehouse)
        return None
    product = level.product if level else variant.product
    return post_adjustment(store, warehouse=warehouse or default_warehouse(store),
        adjusted_on=adjusted_on or timezone.localdate(), reason=reason, notes=notes, actor=actor,
        lines=[{"product": product, "variant": variant, "quantity": delta, "anywhere": bool(variant and not warehouse)}])


def summary(store, start=None, end=None):
    """Posted adjustments per reason: ``[(reason, label, count, value)]`` and the total value (signed)."""
    rows = StockAdjustment.objects.filter(store=store, status=StockAdjustment.POSTED)
    if start:
        rows = rows.filter(adjusted_on__gte=start)
    if end:
        rows = rows.filter(adjusted_on__lte=end)
    found = {}
    for adjustment in rows:
        count, value = found.get(adjustment.reason, (0, Decimal(0)))
        found[adjustment.reason] = (count + 1, value + adjustment.value)
    table = [(key, REASONS[key].label, *found[key]) for key in REASONS if key in found]
    return table, sum((row[3] for row in table), Decimal(0))


@register_backlog
def post_missing(store=None):
    """A posted adjustment whose entry failed is booked by the worker (on the first open day)."""
    rows = StockAdjustment.objects.filter(status=StockAdjustment.POSTED).exclude(value=0)
    if store is not None:
        rows = rows.filter(store=store)
    posted = 0
    for adjustment in unposted(rows, SOURCE):
        if not live_entry(adjustment.store, SOURCE, adjustment.pk) and _book(adjustment, strict=False):
            posted += 1
    return posted
