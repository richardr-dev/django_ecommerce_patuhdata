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
        .exclude(new_on_hand=F("previous_on_hand")).select_related("inventory_level__warehouse", "actor").order_by("created_at", "id"))
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
        rows.append({"move": move, "date": day, "in": max(0, delta), "out": -delta if delta < 0 else 0,
            "balance": running, "warehouse": move.inventory_level.warehouse.name if move.inventory_level_id else "varian"})
    return {"rows": rows, "opening": opening, "closing": running,
        "total_in": sum(r["in"] for r in rows), "total_out": sum(r["out"] for r in rows)}


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
