"""Aturan stok minimum and Usulan pembelian (Odoo's reordering rules and replenishment report).

A rule watches one product or variant, for the whole store or one warehouse. Its **forecast** is

    on hand − reserved + still on order (open purchase orders) − still promised (open sales order backorder)

where "on order" counts only purchase orders bound for the rule's warehouse when it has one, and "promised"
counts only for a store-wide rule (a sales order does not say which warehouse it ships from until its surat
jalan). When the forecast is below ``minimum`` the rule proposes ``maximum − forecast``, rounded up to
``multiple``. Ordering turns the ticked proposals into ordinary purchase orders (``purchasing.create_po``), one
per supplier and warehouse, so nothing here books or moves stock."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum

from .models import PurchaseOrder, PurchaseOrderLine, ReorderRule, Supplier
from .purchasing import create_po

ZERO = Decimal(0)
OPEN_PO = [PurchaseOrder.OPEN, PurchaseOrder.PARTIAL]


class ReorderError(ValueError):
    pass


@dataclass
class Position:
    on_hand: int
    reserved: int
    incoming: int
    outgoing: int

    @property
    def forecast(self):
        return self.on_hand - self.reserved + self.incoming - self.outgoing


@dataclass
class Suggestion:
    rule: ReorderRule
    position: Position
    quantity: int
    supplier: Supplier | None
    unit_cost: Decimal

    @property
    def value(self):
        return self.unit_cost * self.quantity


# ── rules ────────────────────────────────────────────────────────────────────

def save_rule(store, *, product, variant=None, warehouse=None, minimum, maximum, multiple=1, supplier=None,
        lead_days=0, rule=None):
    """Create or change a rule. One rule per product/variant and location."""
    minimum, maximum, multiple, lead_days = int(minimum), int(maximum), int(multiple or 1), int(lead_days or 0)
    if product is None or product.store_id != store.pk:
        raise ReorderError("Pilih produk perusahaan ini.")
    if variant is not None and variant.product_id != product.pk:
        raise ReorderError("Varian bukan milik produk itu.")
    if variant is None and product.variants.filter(is_active=True).exists():
        raise ReorderError(f"{product.name} punya varian; buat aturan per varian (ukuran/warna).")
    if warehouse is not None and (warehouse.store_id != store.pk or not warehouse.is_active):
        raise ReorderError("Gudang bukan gudang aktif perusahaan ini.")
    if supplier is not None and supplier.store_id != store.pk:
        raise ReorderError("Supplier bukan milik perusahaan ini.")
    if minimum < 0 or maximum <= 0 or multiple < 1 or lead_days < 0:
        raise ReorderError("Minimum, maksimum, kelipatan dan waktu tunggu harus angka positif.")
    if maximum < minimum:
        raise ReorderError("Stok maksimum tidak boleh lebih kecil dari minimum.")
    same = ReorderRule.objects.filter(store=store, product=product, variant=variant, warehouse=warehouse)
    if rule is not None:
        same = same.exclude(pk=rule.pk)
    if same.exists():
        raise ReorderError(f"Aturan untuk {variant or product.name}"
            f"{' di ' + warehouse.name if warehouse else ''} sudah ada; ubah yang itu.")
    fields = {"product": product, "variant": variant, "warehouse": warehouse, "minimum": minimum,
        "maximum": maximum, "multiple": multiple, "supplier": supplier, "lead_days": lead_days, "is_active": True}
    if rule is None:
        return ReorderRule.objects.create(store=store, **fields)
    for key, value in fields.items():
        setattr(rule, key, value)
    rule.save()
    return rule


# ── the forecast ─────────────────────────────────────────────────────────────

def position(rule):
    """What the rule's item has, has coming and has promised, at its location."""
    from apps.catalog.models import InventoryLevel
    from apps.tax.models import SalesOrder, SalesOrderLine
    store, warehouse, variant = rule.store, rule.warehouse, rule.variant
    if variant is not None:
        variant.refresh_from_db(fields=["stock", "reserved"])
        if warehouse is not None:
            from apps.orders.variant_stock import at
            on_hand, reserved = at(variant, warehouse), 0   # a reservation does not say where it is
        else:
            on_hand, reserved = variant.stock, variant.reserved
    else:
        levels = InventoryLevel.objects.filter(product=rule.product, warehouse__store=store)
        if warehouse is not None:
            levels = levels.filter(warehouse=warehouse)
        totals = levels.aggregate(on_hand=Sum("on_hand", default=0), reserved=Sum("reserved", default=0))
        on_hand, reserved = totals["on_hand"], totals["reserved"]
    lines = PurchaseOrderLine.objects.filter(po__store=store, po__status__in=OPEN_PO, product=rule.product,
        variant=variant)
    if warehouse is not None:
        lines = lines.filter(po__warehouse=warehouse)
    incoming = lines.aggregate(n=Sum(F("quantity") - F("received_quantity"), default=0))["n"]
    outgoing = 0
    if warehouse is None:
        from apps.tax.sales_orders import reserved_for
        promised = SalesOrderLine.objects.filter(order__store=store, product=rule.product, variant=variant,
            order__status__in=[SalesOrder.STATUS_OPEN, SalesOrder.STATUS_PARTIAL]).aggregate(
            n=Sum(F("quantity") - F("delivered_quantity"), default=0))["n"]
        outgoing = max(0, promised - reserved_for(store, rule.product, variant))   # held units are in ``reserved``
    return Position(on_hand=int(on_hand), reserved=int(reserved), incoming=int(incoming), outgoing=int(outgoing))


def proposed_quantity(rule, forecast):
    """Up to the maximum, in whole multiples; 0 while the forecast is at or above the minimum."""
    if forecast >= rule.minimum:
        return 0
    short = rule.maximum - forecast
    return -(-short // rule.multiple) * rule.multiple


def usual_supplier(rule):
    """The rule's supplier, else whoever this item was last ordered or billed from."""
    if rule.supplier_id:
        return rule.supplier
    line = PurchaseOrderLine.objects.filter(po__store=rule.store, product=rule.product, variant=rule.variant
        ).select_related("po__supplier").order_by("-po__order_date", "-id").first()
    if line is not None:
        return line.po.supplier
    from .models import BillItem
    item = BillItem.objects.filter(bill__store=rule.store, product=rule.product, variant=rule.variant
        ).select_related("bill__supplier").order_by("-bill__bill_date", "-id").first()
    return item.bill.supplier if item is not None and item.bill.supplier_id else None


def usual_cost(rule):
    """The last price ordered at, else the item's cost price."""
    line = PurchaseOrderLine.objects.filter(po__store=rule.store, product=rule.product, variant=rule.variant
        ).order_by("-po__order_date", "-id").first()
    if line is not None:
        return line.unit_cost
    if rule.variant_id and rule.variant.cost_price is not None:
        return rule.variant.cost_price
    return rule.product.cost_price or ZERO


def suggestions(store):
    """Every active rule whose forecast is below its minimum, with what to order."""
    found = []
    for rule in ReorderRule.objects.filter(store=store, is_active=True, product__is_active=True).select_related(
            "product", "variant", "warehouse", "supplier"):
        where = position(rule)
        quantity = proposed_quantity(rule, where.forecast)
        if quantity:
            found.append(Suggestion(rule=rule, position=where, quantity=quantity, supplier=usual_supplier(rule),
                unit_cost=usual_cost(rule)))
    return found


def to_order_count(store):
    return len(suggestions(store))


# ── ordering ─────────────────────────────────────────────────────────────────

@transaction.atomic
def order(store, picks, *, order_date, actor=None):
    """``picks`` is ``[(rule, quantity, supplier, unit_cost)]``. One purchase order per supplier and warehouse,
    expected after the longest lead time among its lines. Returns the purchase orders made."""
    groups = {}
    for rule, quantity, supplier, unit_cost in picks:
        if rule.store_id != store.pk:
            raise ReorderError("Aturan bukan milik perusahaan ini.")
        if supplier is None:
            raise ReorderError(f"Pilih supplier untuk {rule.label}.")
        if int(quantity) <= 0:
            raise ReorderError(f"Jumlah pesanan {rule.label} harus lebih dari nol.")
        if Decimal(unit_cost) < 0:
            raise ReorderError(f"Harga {rule.label} tidak boleh negatif.")
        groups.setdefault((supplier.pk, rule.warehouse_id), (supplier, rule.warehouse, []))[2].append(
            (rule, int(quantity), Decimal(unit_cost)))
    if not groups:
        raise ReorderError("Centang minimal satu barang untuk dipesan.")
    made = []
    for supplier, warehouse, lines in groups.values():
        lead = max(rule.lead_days for rule, _q, _c in lines)
        po = create_po(store, supplier=supplier, order_date=order_date, actor=actor, warehouse=warehouse,
            expected_on=order_date + timedelta(days=lead) if lead else None, notes="Dari usulan pembelian",
            lines=[{"sku": rule.sku, "qty": quantity, "unit_cost": cost} for rule, quantity, cost in lines])
        made.append(po)
    return made
