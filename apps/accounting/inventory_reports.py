"""Inventory reports that read straight from the movement ledger and the books: the stock card of an
item, slow-moving stock, and the inventory close picture (everything that must be settled before the
books of a month are locked)."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Max, Q

from apps.catalog.models import InventoryLevel
from apps.orders.models import InventoryMovement

ZERO = Decimal(0)
OUT_KINDS = ("commit", "deficit", "purchase_return", "consign_out")


def stock_card(store, product, *, start=None, end=None):
    """Every movement that changed the on-hand of ``product`` (reservations do not), oldest first, with
    the signed quantity and the running total across all warehouses. ``opening`` is the stock before ``start``."""
    moves = (InventoryMovement.objects.filter(store=store).filter(Q(inventory_level__product=product) | Q(variant__product=product))
        .exclude(new_on_hand=F("previous_on_hand")).select_related("inventory_level__warehouse", "warehouse", "actor", "order_item__order__invoice")
        .order_by("created_at", "id"))
    rows, running, opening = [], 0, 0
    for move in moves:
        delta = move.new_on_hand - move.previous_on_hand
        day = move.created_at.date()
        if start and day < start:
            running += delta
            opening = running
            continue
        if end and day > end:
            continue
        running += delta
        rows.append({"move": move, "date": day, "url": document_url(move), "in": max(0, delta), "out": -delta if delta < 0 else 0,
            "balance": running, "warehouse": move.inventory_level.warehouse.name if move.inventory_level_id else (
                move.warehouse.name if move.warehouse_id else "varian")})
    return {"rows": rows, "opening": opening, "closing": running,
        "total_in": sum(r["in"] for r in rows), "total_out": sum(r["out"] for r in rows)}


DOCUMENT_URLS = {"stock_adjustment": "merchant-stock-adjustment", "stock_count": "merchant-stock-count",
    "goods_receipt": "merchant-goods-receipt", "bill": "merchant-bill-detail", "credit_note": "merchant-credit-note-detail",
    "delivery": "merchant-delivery-detail", "debit_note": "merchant-debit-note", "stock_request": "merchant-stock-request",
    "ic_transfer": "merchant-intercompany-detail"}


def document_url(move):
    """The page of the document behind a movement (a sale opens its invoice), or "" when there is none to open."""
    from django.urls import NoReverseMatch, reverse
    try:
        if move.order_item_id:
            invoice = getattr(move.order_item.order, "invoice", None)
            return reverse("merchant-invoice", args=[invoice.pk]) if invoice else ""
        name = DOCUMENT_URLS.get(move.source_type)
        return reverse(name, args=[move.source_id]) if name and move.source_id else ""
    except (NoReverseMatch, ValueError):
        return ""


def slow_moving(store, *, days=90, today):
    """Products on the shelf that have not left it for ``days`` days (or never): the cash tied up in them."""
    cutoff = today - timedelta(days=days)
    last = dict(InventoryMovement.objects.filter(store=store, kind__in=OUT_KINDS, inventory_level__isnull=False)
        .values("inventory_level__product_id").annotate(last=Max("created_at")).values_list("inventory_level__product_id", "last"))
    held = {}
    for level in InventoryLevel.objects.filter(warehouse__store=store, on_hand__gt=0).select_related("product"):
        held.setdefault(level.product_id, [level.product, 0])[1] += level.on_hand
    rows = []
    for product_id, (product, units) in held.items():
        when = last.get(product_id)
        if when is None or when.date() <= cutoff:
            cost = product.cost_price or ZERO
            rows.append({"product": product, "units": units, "cost": cost, "value": units * cost,
                "last_out": when.date() if when else None, "idle_days": (today - when.date()).days if when else None,
                "no_cost": product.cost_price is None})
    rows.sort(key=lambda r: -r["value"])
    return {"rows": rows, "value": sum((r["value"] for r in rows), ZERO), "days": days}


def close_picture(store, today):
    """What stands between today's books and a clean inventory close. Each item is
    ``{"title", "figure", "ok", "detail", "url_name"}``; ``ready`` is true when every item is ok."""
    from apps.orders import deficits
    from apps.orders.models import StockRequest

    from . import inventory, purchasing, receipts
    from .models import StockCount
    data = inventory.reconciliation(store)
    minus = deficits.summary(store)
    unbilled = receipts.unbilled(store).prefetch_related("lines")
    unbilled_value = sum((r.value for r in unbilled), ZERO)
    counts = StockCount.objects.filter(store=store, status=StockCount.DRAFT).count()
    sent = StockRequest.objects.filter(store=store, status=StockRequest.SENT).count()
    late = purchasing.overdue(store, today).count()
    items = [
        {"title": "Nilai persediaan di buku", "figure": data["book"], "kind": "money", "ok": True,
            "detail": "Saldo akun Persediaan barang.", "url_name": "merchant-inventory-value"},
        {"title": "Selisih buku dan stok", "figure": data["difference"], "kind": "money", "ok": data["difference"] == 0,
            "detail": "Buku dikurangi nilai stok, barang dalam perjalanan, dan stok minus. Harus nol.", "url_name": "merchant-inventory-value"},
        {"title": "Stok minus (nilai sementara)", "figure": minus["units"], "kind": "units", "ok": minus["units"] == 0,
            "detail": f"{minus['items']} barang, nilai sementara Rp{minus['value']:,.0f}".replace(",", "."), "url_name": "merchant-stock-minus"},
        {"title": "Penerimaan belum ditagih (GRNI)", "figure": unbilled_value, "kind": "money", "ok": True,
            "detail": f"{len(unbilled)} penerimaan menunggu tagihan supplier. Normal, tapi tinjau yang lama.", "url_name": "merchant-goods-receipts"},
        {"title": "Stok opname belum disetujui", "figure": counts, "kind": "units", "ok": counts == 0,
            "detail": "Hitungan yang masih berjalan menahan selisih yang belum dibukukan.", "url_name": "merchant-stock-counts"},
        {"title": "Kiriman antar gudang dalam perjalanan", "figure": sent, "kind": "units", "ok": sent == 0,
            "detail": "Sudah keluar dari satu gudang, belum diterima gudang tujuan.", "url_name": "merchant-stock-requests"},
        {"title": "Pesanan pembelian terlambat", "figure": late, "kind": "units", "ok": late == 0,
            "detail": "Barang diperkirakan sudah tiba tetapi belum diterima semua.", "url_name": "merchant-purchase-orders"},
    ]
    return {"items": items, "ready": all(item["ok"] for item in items), "reconciliation": data}


# Mutasi persediaan: which kinds of movement fall in which column, for the units part.
MUTATION_COLUMNS = [
    ("purchases", "Pembelian", ("receipt",)),
    ("returns", "Retur & batal jual", ("return",)),
    ("sales", "Penjualan", ("commit", "issue", "deficit_cover")),
    ("supplier_returns", "Retur ke supplier", ("purchase_return",)),
    ("mitra", "Mitra (konsinyasi)", ("consign_out", "consign_in")),
    ("transfers", "Pindah gudang", ("transfer_in", "transfer_out")),
    ("adjustments", "Penyesuaian & opname", ("adjust", "opname")),
]
_COLUMN_OF = {kind: key for key, _label, kinds in MUTATION_COLUMNS for kind in kinds}

# The documents that move Persediaan (1400), for the rupiah part.
LEDGER_SOURCES = {
    "opening": "Saldo awal pembukuan", "bill": "Tagihan supplier", "goods_receipt": "Penerimaan barang (GRNI)",
    "invoice": "Penjualan (HPP)", "delivery": "Surat jalan", "delivery_cancel": "Surat jalan dibatalkan",
    "credit_note": "Nota kredit / retur penjualan", "return_exchange": "Tukar barang", "debit_note": "Nota debit / retur ke supplier",
    "stock_count": "Stok opname", "stock_adjustment": "Penyesuaian stok", "stock_revalue": "Koreksi HPP stok minus",
    "landed_cost": "Biaya perolehan (ongkos angkut, bea masuk)",
    "deficit": "Stok minus", "consignment": "Konsinyasi mitra", "consignment_move": "Kirim ke / kembali dari mitra",
    "ic_out": "Transfer antar brand (keluar)", "ic_in": "Transfer antar brand (masuk)", "ic_transfer": "Transfer antar brand",
    "stock_request": "Barang hilang dalam kiriman", "journal": "Jurnal manual", "jtemplate": "Jurnal berulang",
    "": "Jurnal tanpa dokumen",
}


def mutation(store, start, end, *, warehouse=None):
    """Mutasi persediaan for ``start``..``end``. Units per item (product, or variant) from the movement ledger:
    ``opening`` + one signed figure per column of ``MUTATION_COLUMNS`` = ``closing``. Rupiah from the books: the
    Persediaan (1400) balance at the start, what each kind of document added and took off, and the balance at the
    end. The two halves are separate on purpose: units come from the shelf, value from the ledger, and the inventory
    controls tie them. With ``warehouse`` only that location's units count (a variant's by the movement's warehouse;
    variant movements from before variants were stocked per warehouse have none and are left out)."""
    from django.db.models import Sum

    from .ledger import INVENTORY
    from .models import JournalLine

    delta = F("new_on_hand") - F("previous_on_hand")
    moves = InventoryMovement.objects.filter(store=store).exclude(new_on_hand=F("previous_on_hand"))
    if warehouse is not None:
        moves = moves.filter(Q(inventory_level__warehouse=warehouse) | Q(variant__isnull=False, warehouse=warehouse))
    names = ("inventory_level__product_id", "variant_id", "variant__product_id")
    items = {}

    def item(row):
        product_id = row["inventory_level__product_id"] or row["variant__product_id"]
        key = (product_id, row["variant_id"])
        if key not in items:
            items[key] = {"key": key, "opening": 0, "closing": 0, **{c: 0 for c, _l, _k in MUTATION_COLUMNS}}
        return items[key]

    for row in moves.filter(created_at__date__lt=start).values(*names).annotate(total=Sum(delta)):
        item(row)["opening"] += row["total"] or 0
    for row in moves.filter(created_at__date__range=(start, end)).values(*names, "kind").annotate(total=Sum(delta)):
        item(row)[_COLUMN_OF.get(row["kind"], "adjustments")] += row["total"] or 0
    from apps.catalog.models import Product, ProductVariant
    products = Product.objects.in_bulk({key[0] for key in items})
    variants = ProductVariant.objects.in_bulk({key[1] for key in items if key[1]})
    rows = []
    for (product_id, variant_id), row in items.items():
        row["closing"] = row["opening"] + sum(row[c] for c, _l, _k in MUTATION_COLUMNS)
        if not any(row[c] for c in ("opening", "closing", *_COLUMN_OF.values())):
            continue
        product, variant = products[product_id], variants.get(variant_id)
        row["name"] = f"{product.name} · {variant.name}" if variant else product.name
        row["sku"] = (variant.sku if variant and variant.sku else product.sku)
        row["product"] = product
        rows.append(row)
    rows.sort(key=lambda r: (r["name"].lower(), r["sku"]))
    used = [(c, label) for c, label, _k in MUTATION_COLUMNS if any(r[c] for r in rows)]
    unit_totals = {c: sum(r[c] for r in rows) for c in ("opening", "closing", *(c for c, _l, _k in MUTATION_COLUMNS))}
    for row in [*rows, unit_totals]:
        row["cells"] = [row[c] for c, _label in used]  # the columns that hold anything, in order, for the screen

    lines = JournalLine.objects.filter(entry__store=store, account__code=INVENTORY)
    before = lines.filter(entry__date__lt=start).aggregate(d=Sum("debit", default=ZERO), c=Sum("credit", default=ZERO))
    opening_value = before["d"] - before["c"]
    groups = {}
    for line in lines.filter(entry__date__range=(start, end)).values(
            "entry__source_type", "entry__reverses__source_type").annotate(d=Sum("debit"), c=Sum("credit")):
        kind = line["entry__reverses__source_type"] if line["entry__source_type"] == "reversal" else line["entry__source_type"]
        group = groups.setdefault(kind or "", {"label": LEDGER_SOURCES.get(kind or "", (kind or "").replace("_", " ").capitalize()),
            "kind": kind or "", "debit": ZERO, "credit": ZERO})
        group["debit"] += line["d"] or ZERO
        group["credit"] += line["c"] or ZERO
    value_rows = sorted(groups.values(), key=lambda g: -(g["debit"] + g["credit"]))
    debit = sum((g["debit"] for g in value_rows), ZERO)
    credit = sum((g["credit"] for g in value_rows), ZERO)
    return {"start": start, "end": end, "warehouse": warehouse, "rows": rows, "columns": used, "units": unit_totals,
        "value": {"opening": opening_value, "rows": value_rows, "debit": debit, "credit": credit,
            "closing": opening_value + debit - credit}}
