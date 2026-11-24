"""Pesanan penjualan (sales order) → surat jalan (delivery) → invoice, with partial deliveries and
backorders.

A sales order books nothing. It *reserves* the free stock it can when it is made (``reserve``): units on a
warehouse level for a product, store-wide for a variant, each with a ``reserve`` movement, so another sale
cannot take what was promised. Short stock reserves what there is; "Cadangkan stok" tops it up later. A
delivery releases the units it ships before taking them; closing or cancelling releases the rest. A delivery (surat jalan) is the stock document: the units leave the shelf (movement kind
``issue``) and their cost moves from Persediaan (1400) to "Barang terkirim belum ditagih" (1410), so
the shelf and the books still agree the moment the goods go out. An invoice made from one or more
deliveries books the sale (Piutang / Penjualan / PPN) and the cost of those units as HPP against
1410, never against Persediaan again. What is ordered but not delivered is the backorder; closing an
order drops it. Numbers: ``SO/<year>/<n>`` and ``SJ/<year>/<n>``.

Only goods (products or one of their variants) can be ordered here; a service is invoiced directly on the
invoice form. A delivery may name the warehouse it leaves from; a variant's units leave its own rows there."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import (
    DELIVERED,
    INVENTORY,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from apps.orders.stock import StockError, stock_in, stock_out

from .models import (
    Delivery,
    DeliveryLine,
    Invoice,
    Quote,
    SalesOrder,
    SalesOrderLine,
    SalesOrderReservation,
)
from .numbering import next_number
from .sales import clean_lines, create_sales_invoice, price_lines
from .services import InvoiceError

ZERO = Decimal(0)


class SalesOrderError(ValueError):
    pass


# ── the order ────────────────────────────────────────────────────────────────

@transaction.atomic
def create_sales_order(store, *, lines, buyer_name, customer=None, issued_on=None, expected_on=None,
        company_name="", buyer_npwp="", phone="", address="", notes="", discount=0, shipping=0,
        quote=None, actor=None):
    try:
        lines = clean_lines(lines)
    except InvoiceError as error:
        raise SalesOrderError(str(error)) from None
    if any(not line.get("product") for line in lines):
        raise SalesOrderError("Pesanan penjualan hanya untuk barang; jasa ditagih langsung lewat invoice.")
    issued_on = issued_on or timezone.localdate()
    discount, shipping = Decimal(discount or 0), Decimal(shipping or 0)
    subtotal, tax, total = price_lines(lines, discount, shipping, store=store)
    order = SalesOrder.objects.create(store=store, customer=customer, number=next_number(store, "SO", issued_on),
        issued_on=issued_on, expected_on=expected_on, buyer_name=(buyer_name or (customer.name if customer else "")
        or "Pelanggan")[:160], company_name=(company_name or "")[:160], buyer_npwp=(buyer_npwp or "")[:16],
        phone=(phone or "")[:30], address=address or "", notes=notes or "", discount=discount, shipping=shipping,
        subtotal=subtotal, tax=tax, total=total, quote=quote, created_by=actor)
    for line in lines:
        product, variant = line["product"], line.get("variant")
        SalesOrderLine.objects.create(order=order, product=product, variant=variant, description=line["description"],
            sku=variant.sku if variant else product.sku, quantity=int(line["qty"]), unit_price=line["unit_price"],
            discount=line["discount"], unit=line["unit"])
    if quote is not None and quote.status not in {Quote.STATUS_INVOICED}:
        Quote.objects.filter(pk=quote.pk).update(status=Quote.STATUS_ACCEPTED)
    reserve(order, actor=actor)
    return order


# ── reservations ─────────────────────────────────────────────────────────────

def _movement(order, *, kind, quantity, actor, level=None, variant=None, before_reserved, after_reserved):
    from apps.orders.inventory import record_movement
    holder = variant or level
    on_hand = variant.stock if variant is not None else level.on_hand
    record_movement(store=order.store, kind=kind, quantity=quantity, inventory_level=level, variant=variant,
        reason=f"{'Cadangkan' if kind == 'reserve' else 'Lepas'} untuk pesanan {order.number}"[:200],
        previous_on_hand=on_hand, new_on_hand=on_hand, previous_reserved=before_reserved,
        new_reserved=after_reserved, actor=actor, source_type="sales_order", source_id=str(order.pk))
    return holder


@transaction.atomic
def reserve(order, *, actor=None):
    """Hold the free stock each open line still needs (what is ordered, less delivered, less already held).
    Returns the units newly reserved. Untracked products (no stock level) hold nothing."""
    from apps.catalog.models import ProductVariant
    from apps.orders.stock import _levels
    if order.status not in {SalesOrder.STATUS_OPEN, SalesOrder.STATUS_PARTIAL}:
        return 0
    held = 0
    for line in order.lines.select_related("product", "variant").prefetch_related("reservations"):
        need = line.remaining - line.reserved_quantity
        if need <= 0:
            continue
        if line.variant_id:
            variant = ProductVariant.objects.select_for_update().get(pk=line.variant_id)
            take = min(need, variant.stock - variant.reserved)
            if take > 0:
                before = variant.reserved
                variant.reserved += take
                variant.save(update_fields=["reserved"])
                _movement(order, kind="reserve", quantity=take, actor=actor, variant=variant,
                    before_reserved=before, after_reserved=variant.reserved)
                SalesOrderReservation.objects.create(line=line, variant=variant, quantity=take)
                held += take
            continue
        for level in _levels(line.product):
            take = min(need, level.on_hand - level.reserved)
            if take <= 0:
                continue
            before = level.reserved
            level.reserved += take
            level.save(update_fields=["reserved", "updated_at"])
            _movement(order, kind="reserve", quantity=take, actor=actor, level=level, before_reserved=before,
                after_reserved=level.reserved)
            SalesOrderReservation.objects.create(line=line, inventory_level=level, quantity=take)
            held += take
            need -= take
            if not need:
                break
    return held


def release(line, quantity=None, *, actor=None):
    """Give back up to ``quantity`` (default: all) of what ``line`` holds, newest first. Returns the units released."""
    from apps.catalog.models import InventoryLevel, ProductVariant
    order = line.order
    left = line.reserved_quantity if quantity is None else quantity
    freed = 0
    for row in SalesOrderReservation.objects.select_for_update().filter(line=line).order_by("-id"):
        if left <= 0:
            break
        take = min(left, row.quantity)
        if row.variant_id:
            holder = ProductVariant.objects.select_for_update().get(pk=row.variant_id)
            before = holder.reserved
            holder.reserved = max(0, holder.reserved - take)
            holder.save(update_fields=["reserved"])
            _movement(order, kind="release", quantity=take, actor=actor, variant=holder, before_reserved=before,
                after_reserved=holder.reserved)
        else:
            holder = InventoryLevel.objects.select_for_update().get(pk=row.inventory_level_id)
            before = holder.reserved
            holder.reserved = max(0, holder.reserved - take)
            holder.save(update_fields=["reserved", "updated_at"])
            _movement(order, kind="release", quantity=take, actor=actor, level=holder, before_reserved=before,
                after_reserved=holder.reserved)
        if take == row.quantity:
            row.delete()
        else:
            row.quantity -= take
            row.save(update_fields=["quantity"])
        left -= take
        freed += take
    return freed


def reserved_for(store, product, variant=None):
    """Units held by open sales orders for an item (the reorder forecast subtracts them only once)."""
    from django.db.models import Sum
    return SalesOrderReservation.objects.filter(line__order__store=store, line__product=product,
        line__variant=variant).aggregate(n=Sum("quantity", default=0))["n"]


def from_quote(quote, *, actor=None):
    """A sales order with the quote's lines and buyer."""
    if hasattr(quote, "sales_order"):
        raise SalesOrderError(f"Penawaran ini sudah menjadi pesanan {quote.sales_order.number}.")
    lines = [{"product": item.product, "variant": item.variant, "qty": item.quantity, "unit_price": item.unit_price,
        "description": item.description} for item in quote.items.select_related("product", "variant")]
    return create_sales_order(quote.store, lines=lines, buyer_name=quote.buyer_name, customer=quote.customer,
        company_name=quote.company_name, buyer_npwp=quote.buyer_npwp, phone=quote.phone, address=quote.address,
        notes=quote.notes, discount=quote.discount, shipping=quote.shipping, quote=quote, actor=actor)


def refresh_status(order):
    """Open, partly delivered, fully delivered: from the lines. Closed and cancelled stay."""
    if order.status in {SalesOrder.STATUS_CLOSED, SalesOrder.STATUS_CANCELLED}:
        return order.status
    lines = list(order.lines.all())
    delivered = sum(line.delivered_quantity for line in lines)
    ordered = sum(line.quantity for line in lines)
    status = (SalesOrder.STATUS_FULFILLED if delivered >= ordered else
        SalesOrder.STATUS_PARTIAL if delivered else SalesOrder.STATUS_OPEN)
    if status != order.status:
        order.status = status
        order.save(update_fields=["status"])
    return status


def close_order(order, *, actor=None):
    """Stop waiting for the rest: the backorder is dropped, what was delivered stays."""
    if order.status in {SalesOrder.STATUS_CLOSED, SalesOrder.STATUS_CANCELLED}:
        return order
    for line in order.lines.all():
        release(line, actor=actor)   # the backorder no longer holds stock
    if order.lines.filter(delivered_quantity__gt=0).exists():
        order.status = SalesOrder.STATUS_CLOSED
    else:
        order.status = SalesOrder.STATUS_CANCELLED
    order.save(update_fields=["status"])
    return order


def backorder(store):
    """Lines still waiting to be delivered, oldest order first."""
    return SalesOrderLine.objects.filter(order__store=store, order__status__in=[SalesOrder.STATUS_OPEN,
        SalesOrder.STATUS_PARTIAL]).exclude(delivered_quantity__gte=models_F("quantity")).select_related(
        "order", "product").order_by("order__issued_on", "order__number")


def models_F(name):
    from django.db.models import F
    return F(name)


# ── deliveries (surat jalan) ─────────────────────────────────────────────────

def post_delivery(delivery, *, strict=False, actor=None):
    value = delivery.value
    if not value:
        return None
    return post_entry(delivery.store, date=delivery.delivered_on, strict=strict, actor=actor,
        source=("delivery", delivery.pk),
        memo=f"Surat jalan {delivery.number} · {delivery.sales_order.buyer_name}",
        amounts={DELIVERED: value, INVENTORY: -value})


@transaction.atomic
def create_delivery(order, *, quantities, delivered_on=None, courier="", tracking_number="", notes="", actor=None,
        warehouse=None):
    """``quantities`` is ``{line id: units}``. Each line may not go beyond what is still open on it.
    The units leave the shelf now (from ``warehouse`` only, when given), at the variant's or product's
    current cost."""
    from apps.orders.stock import check_warehouse
    try:
        check_warehouse(order.store, warehouse)
    except StockError as error:
        raise SalesOrderError(str(error)) from None
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in {SalesOrder.STATUS_OPEN, SalesOrder.STATUS_PARTIAL}:
        raise SalesOrderError("Pesanan ini tidak lagi menunggu pengiriman.")
    delivered_on = delivered_on or timezone.localdate()
    wanted = []
    for line in order.lines.select_for_update(of=("self",)).select_related("product", "variant"):
        qty = int(quantities.get(line.pk) or quantities.get(str(line.pk)) or 0)
        if qty <= 0:
            continue
        if qty > line.remaining:
            raise SalesOrderError(f"{line.description}: kirim {qty} melebihi sisa pesanan {line.remaining}.")
        cost = line.variant.cost_price if line.variant and line.variant.cost_price is not None else line.product.cost_price
        if cost is None:
            raise SalesOrderError(f"Harga pokok {line.description} belum diisi; isi dulu agar HPP benar.")
        wanted.append((line, qty, cost))
    if not wanted:
        raise SalesOrderError("Isi jumlah yang dikirim untuk minimal satu barang.")
    delivery = Delivery.objects.create(store=order.store, sales_order=order, number=next_number(order.store, "SJ", delivered_on),
        delivered_on=delivered_on, courier=(courier or "").strip().lower()[:40],
        tracking_number=(tracking_number or "").strip()[:100], notes=notes or "", warehouse=warehouse,
        created_by=actor)
    for line, qty, cost in wanted:
        release(line, qty, actor=actor)   # the order's own reservation makes way for the units it ships
        try:
            stock_out(store=order.store, product=line.product, variant=line.variant, quantity=qty, kind="issue",
                reason=f"Surat jalan {delivery.number}", source=("delivery", str(delivery.pk)), unit_cost=cost,
                actor=actor, warehouse=warehouse)
        except StockError as error:
            raise SalesOrderError(str(error)) from None
        DeliveryLine.objects.create(delivery=delivery, order_line=line, product=line.product, variant=line.variant,
            quantity=qty, unit_cost=cost)
        line.delivered_quantity += qty
        line.save(update_fields=["delivered_quantity"])
    post_delivery(delivery, strict=True, actor=actor)
    refresh_status(order)
    return delivery


@transaction.atomic
def cancel_delivery(delivery, *, actor=None):
    """The parcel never went: the units return to the shelf and the entry is reversed."""
    delivery = Delivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.status == Delivery.STATUS_CANCELLED:
        return delivery
    if delivery.invoice_id:
        raise SalesOrderError("Surat jalan ini sudah ditagih; batalkan invoicenya lebih dulu.")
    memo = f"Pembatalan surat jalan {delivery.number}"
    for line in delivery.lines.select_related("order_line", "product", "variant"):
        stock_in(store=delivery.store, product=line.product, variant=line.variant, quantity=line.quantity,
            kind="return", reason=memo, source=("delivery_cancel", str(delivery.pk)), unit_cost=line.unit_cost,
            actor=actor, warehouse=delivery.warehouse)
        line.order_line.delivered_quantity -= line.quantity
        line.order_line.save(update_fields=["delivered_quantity"])
    reverse_source(delivery.store, "delivery", delivery.pk, actor=actor, memo=memo)
    delivery.status = Delivery.STATUS_CANCELLED
    delivery.save(update_fields=["status"])
    order = delivery.sales_order
    if order.status in {SalesOrder.STATUS_PARTIAL, SalesOrder.STATUS_FULFILLED}:
        refresh_status(order)
        reserve(order, actor=actor)   # the units are back and still owed: hold them again
    return delivery


# ── the invoice from deliveries ──────────────────────────────────────────────

def _share(amount, weights):
    """Split ``amount`` over ``weights`` to the rupiah, the remainder on the largest."""
    total = sum(weights, ZERO)
    if not amount or not total:
        return [ZERO] * len(weights)
    parts = [(amount * w / total).quantize(Decimal(1), rounding=ROUND_HALF_UP) for w in weights]
    parts[max(range(len(parts)), key=lambda i: weights[i])] += amount - sum(parts, ZERO)
    return parts


@transaction.atomic
def invoice_deliveries(order, deliveries, *, issued_on=None, on_credit=True, due_date=None, notes="", actor=None):
    """One invoice for these deliveries of the order: the delivered units at the order's prices, the
    order's discount shared by value, the order's shipping on its first invoice."""
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    chosen = list(Delivery.objects.select_for_update().filter(pk__in=[d.pk for d in deliveries], sales_order=order,
        status=Delivery.STATUS_SHIPPED, invoice__isnull=True).prefetch_related("lines__order_line__product"))
    if not chosen or len(chosen) != len({d.pk for d in deliveries}):
        raise SalesOrderError("Pilih surat jalan dari pesanan ini yang belum ditagih.")
    per_line, cost_total, cost_units = {}, {}, {}
    for delivery in chosen:
        for row in delivery.lines.all():
            per_line[row.order_line_id] = per_line.get(row.order_line_id, 0) + row.quantity
            key = (row.product_id, row.variant_id)   # a size's cost is its own
            cost_total[key] = cost_total.get(key, ZERO) + row.unit_cost * row.quantity
            cost_units[key] = cost_units.get(key, 0) + row.quantity
    lines, order_lines = [], {line.pk: line for line in order.lines.select_related("product", "variant")}
    for line_id, qty in per_line.items():
        line = order_lines[line_id]
        discount = (line.discount * qty / line.quantity).quantize(Decimal(1), rounding=ROUND_HALF_UP) if line.discount else ZERO
        lines.append({"product": line.product, "variant": line.variant, "description": line.description, "qty": qty,
            "unit_price": line.unit_price, "discount": discount, "unit": line.unit})
    gross = [Decimal(l["qty"]) * l["unit_price"] - l["discount"] for l in lines]
    order_gross = sum((Decimal(l.quantity) * l.unit_price - l.discount for l in order_lines.values()), ZERO)
    share = (sum(gross, ZERO) / order_gross) if order_gross else ZERO
    discount = (order.discount * share).quantize(Decimal(1), rounding=ROUND_HALF_UP) if order.discount else ZERO
    shipping = order.shipping if order.shipping and not order.invoices.exclude(status=Invoice.STATUS_VOID).exists() else ZERO
    costs = {pid: (cost_total[pid] / cost_units[pid]).quantize(Decimal("0.01")) for pid in cost_total}
    invoice = create_sales_invoice(order.store, lines=lines, buyer_name=order.buyer_name, customer=order.customer,
        company_name=order.company_name, buyer_npwp=order.buyer_npwp, phone=order.phone or "-",
        address=order.address or "-", notes=notes or order.notes, discount=discount, shipping=shipping,
        on_credit=on_credit, due_date=due_date, issued_on=issued_on, actor=actor,
        courier=chosen[0].courier, tracking_number=chosen[0].tracking_number,
        delivered_costs=costs, sales_order=order)
    Delivery.objects.filter(pk__in=[d.pk for d in chosen]).update(invoice=invoice)
    for line_id, qty in per_line.items():
        line = order_lines[line_id]
        line.invoiced_quantity += qty
        line.save(update_fields=["invoiced_quantity"])
    return invoice


def release_invoice(invoice):
    """Called when an invoice made from deliveries is voided: its deliveries can be invoiced again."""
    for delivery in invoice.deliveries.prefetch_related("lines__order_line"):
        for row in delivery.lines.all():
            row.order_line.invoiced_quantity = max(0, row.order_line.invoiced_quantity - row.quantity)
            row.order_line.save(update_fields=["invoiced_quantity"])
    invoice.deliveries.update(invoice=None)


def delivered_not_invoiced(store):
    """Cost of goods that left on a surat jalan and are not on an invoice yet: what 1410 should hold."""
    total = ZERO
    for row in DeliveryLine.objects.filter(delivery__store=store, delivery__status=Delivery.STATUS_SHIPPED,
            delivery__invoice__isnull=True):
        total += row.unit_cost * row.quantity
    return total


@register_backlog
def post_unposted_deliveries():
    posted = 0
    for delivery in unposted(Delivery.objects.filter(status=Delivery.STATUS_SHIPPED), "delivery"):
        posted += bool(post_delivery(delivery))
    return posted
