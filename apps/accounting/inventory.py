"""Nilai persediaan: what the shelves hold, valued at cost, set against Persediaan in the books.

Every sale takes stock off the shelf and books its cost against Persediaan (1400) in one step, and
receiving goods does the reverse, so the two move together. They can still differ: stock that
was on the shelf before the books were started has no opening balance, a supplier bill is booked
before its goods arrive, or old invoices were issued without enough stock. This page shows that
difference and can book the adjustment."""
from decimal import Decimal

from django.db.models import Sum

from apps.catalog.models import InventoryLevel, Product, ProductVariant
from apps.settlements.models import ConsignmentStock

from .ledger import CONSIGNED, DRAWINGS, EQUITY, INVENTORY, account_balances, post_journal
from .models import BillItem

ZERO = Decimal(0)


def _cost(item, product):
    cost = getattr(item, "cost_price", None)
    return cost if cost is not None else product.cost_price


def stock_valuation(store):
    """``{"rows": [...], "shelf_value", "mitra_value", "missing_cost"}``. A product with
    variants is counted by its variants, so no unit is counted twice."""
    shelf = {row["product_id"]: row["total"] for row in InventoryLevel.objects.filter(
        warehouse__store=store).values("product_id").annotate(total=Sum("on_hand"))}
    mitra = {}
    for row in ConsignmentStock.objects.filter(product__store=store).values(
            "product_id", "variant_id").annotate(total=Sum("quantity")):
        mitra[(row["product_id"], row["variant_id"])] = row["total"] or 0
    variants = {}
    for variant in ProductVariant.objects.filter(product__store=store, is_active=True):
        variants.setdefault(variant.product_id, []).append(variant)
    rows, missing = [], 0
    for product in Product.objects.filter(store=store):
        holders = variants.get(product.pk) or [None]
        for variant in holders:
            on_shelf = variant.stock if variant else shelf.get(product.pk, 0)
            at_mitra = mitra.get((product.pk, variant.pk if variant else None), 0)
            if not on_shelf and not at_mitra:
                continue
            cost = _cost(variant, product) if variant else product.cost_price
            if cost is None:
                missing += 1
                cost = ZERO
            rows.append({"product": product, "variant": variant,
                "name": f"{product.name} · {variant.name}" if variant else product.name,
                "sku": (variant.sku if variant and variant.sku else product.sku),
                "shelf": on_shelf, "mitra": at_mitra, "cost": cost,
                "shelf_value": on_shelf * cost, "mitra_value": at_mitra * cost,
                "no_cost": cost == ZERO})
    rows.sort(key=lambda row: -(row["shelf_value"] + row["mitra_value"]))
    return {"rows": rows, "shelf_value": sum((r["shelf_value"] for r in rows), ZERO),
        "mitra_value": sum((r["mitra_value"] for r in rows), ZERO), "missing_cost": missing}


def goods_in_transit(store):
    """Goods on supplier bills (already booked to Persediaan) that have not been received."""
    total = ZERO
    for item in BillItem.objects.filter(bill__store=store, product__isnull=False).exclude(
            bill__status="void").select_related("bill"):
        waiting = item.quantity - item.received_quantity
        if waiting > 0:
            total += waiting * item.unit_price
    return total


def reconciliation(store):
    """Stock value against the books: ``difference`` is books minus what the shelves explain."""
    valuation = stock_valuation(store)
    ledger = {a.code: a.balance for a in account_balances(store)}
    book, book_mitra = ledger.get(INVENTORY, ZERO), ledger.get(CONSIGNED, ZERO)
    transit = goods_in_transit(store)
    # Cost is a moving average kept to two decimals, so value and books drift by a fraction of a
    # rupiah per unit: that is rounding, not a difference to book.
    units = sum(row["shelf"] + row["mitra"] for row in valuation["rows"])
    noise = abs(units) * Decimal("0.005") + 1

    def settle(value):
        return ZERO if abs(value) <= noise else value
    # Stok minus: HPP for units the shelf did not have is already booked, so the books run below
    # the shelf value by the open deficits at their provisional cost.
    from apps.orders.deficits import summary as deficit_summary
    minus = deficit_summary(store)
    from apps.orders.stock_requests import in_transit_value
    moving = in_transit_value(store)  # sent between warehouses, not received yet: still Persediaan
    return {**valuation, "book": book, "book_mitra": book_mitra, "in_transit": transit, "moving": moving,
        "deficit_units": minus["units"], "deficit_value": minus["value"],
        "difference": settle(book - (valuation["shelf_value"] + transit + moving - minus["value"])),
        "difference_mitra": settle(book_mitra - valuation["mitra_value"])}


CONTRA = {"opening": (EQUITY, "Saldo awal persediaan"),
    "loss": ("5100", "Selisih persediaan"), "drawings": (DRAWINGS, "Persediaan diambil pemilik")}


def adjust_books(store, *, date, contra="opening", actor=None):
    """Book the difference so Persediaan (and Persediaan di mitra) equal the stock value.
    ``contra`` says where the other side goes. Returns the entries posted."""
    data = reconciliation(store)
    account, label = CONTRA[contra]
    posted = []
    for code, diff, name in ((INVENTORY, data["difference"], "Persediaan barang"),
            (CONSIGNED, data["difference_mitra"], "Persediaan di mitra")):
        if diff == 0:
            continue
        # books are higher than the shelves explain: take the excess off; lower: add the shortfall
        lines = [(code, ZERO, diff), (account, diff, ZERO)] if diff > 0 else \
            [(code, -diff, ZERO), (account, ZERO, -diff)]
        posted.append(post_journal(store, date=date, memo=f"{label}: penyesuaian {name} ke nilai stok",
            lines=lines, actor=actor))
    return posted
