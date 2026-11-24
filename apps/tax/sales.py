"""Writing a sales invoice by hand: the one place that turns lines and buyer details into an
order, its stock movement, and a tax invoice booked on the ledger. The invoice form, quotes
(penawaran), recurring invoices and invoice edits all use it, so they price, tax and post
identically.

A line is ``{"product": Product or None, "description": str, "qty", "unit_price", "discount"}``.
A product line moves stock and books its cost; a line without a product (a service, a delivery
fee) is only revenue. ``discount`` is the line's own discount in rupiah."""
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.orders.models import Order, OrderItem, OrderStatusEvent

from .models import Invoice, InvoiceItem
from .services import InvoiceError, exclusive_price, issue_invoice_for_order, money, vat_for

ZERO = Decimal(0)


class StockShortage(InvoiceError):
    """An invoice asked for more than the shelf holds. The invoice is not made: the books would
    show goods leaving that never did."""


def line_net(line):
    """What a line comes to: quantity times price less its own discount (never below zero)."""
    gross = Decimal(line["qty"]) * Decimal(line["unit_price"])
    return max(gross - Decimal(line.get("discount") or 0), ZERO)


def price_lines(lines, discount=0, shipping=0, *, store, is_pkp=None):
    """``(subtotal, tax, total)`` of ``lines``, with PPN on what is left after the invoice's own
    discount. ``is_pkp`` overrides the store's current tax status (an issued invoice keeps its)."""
    subtotal = sum((line_net(line) for line in lines), ZERO)
    discount, shipping = Decimal(discount or 0), Decimal(shipping or 0)
    base = max(subtotal - discount, ZERO)
    _, tax = vat_for(store, base, is_pkp=is_pkp)
    return subtotal, tax, base + tax + shipping


def from_inclusive(store, lines, discount=0):
    """Lines typed with prices that already include PPN, as the books need them: ``(lines, discount,
    rounding)``. Each unit price and discount is backed out to before PPN (``exclusive_price``); the cents
    that do not come back exactly are ``rounding`` (to Pembulatan 4950), so the invoice total is exactly
    what was typed. A seller who charges no PPN gets the lines back unchanged."""
    discount = Decimal(discount or 0)
    if not vat_for(store, Decimal(100))[1]:
        return lines, discount, ZERO
    converted = [{**line, "unit_price": exclusive_price(store, line["unit_price"]),
        "discount": exclusive_price(store, line.get("discount") or 0)} for line in lines]
    converted_discount = exclusive_price(store, discount)
    typed = max(sum((line_net(line) for line in lines), ZERO) - discount, ZERO)
    _subtotal, _tax, total = price_lines(converted, converted_discount, 0, store=store)
    return converted, converted_discount, money(typed - total)


def clean_lines(lines):
    """Drop empty rows and refuse a half-filled one."""
    kept = []
    for line in lines:
        product, variant = line.get("product"), line.get("variant")
        if variant is not None:
            if product is None:
                product = variant.product
            if variant.product_id != product.pk:
                raise InvoiceError(f"Varian {variant.sku} bukan milik {product.name}.")
        text = (line.get("description") or "").strip()
        if not product and not text:
            continue
        qty, price = Decimal(line["qty"]), Decimal(line["unit_price"])
        if qty <= 0:
            raise InvoiceError(f"Jumlah “{text or product.name}” harus lebih dari nol.")
        if price < 0:
            raise InvoiceError(f"Harga “{text or product.name}” tidak boleh negatif.")
        if product and qty != qty.to_integral_value():
            raise InvoiceError(f"Jumlah {product.name} harus bilangan bulat.")
        discount = Decimal(line.get("discount") or 0)
        if discount < 0 or discount > qty * price:
            raise InvoiceError(f"Diskon “{text or product.name}” melebihi harga barisnya.")
        from apps.catalog.units import factor as unit_factor
        unit = (line.get("unit") or "").strip()[:20]
        kept.append({"product": product, "variant": variant, "unit_factor": unit_factor(product, unit) if product else 1,
            "description": text or (f"{product.name} / {variant.name}" if variant else product.name), "qty": qty,
            "unit_price": price, "discount": discount, "unit": (line.get("unit") or "").strip()[:20],
            "note": (line.get("note") or "").strip()[:300],
            "code": (line.get("code") or "").strip()[:60]})
    if not kept:
        raise InvoiceError("Tambahkan minimal satu barang atau jasa.")
    return kept


def _units(line):
    """Base units a line moves: quantity × the factor of its unit (2 dus of 12 = 24)."""
    return int(line["qty"]) * int(line.get("unit_factor") or 1)


def _wants(lines):
    return [(line["product"], _units(line)) for line in lines if line.get("product") and not line.get("variant")]


def _variant_shortages(lines, warehouse=None):
    """``[(variant, wanted, available)]`` for the sizes or colours the shelf (or ``warehouse``) cannot cover."""
    need = {}
    for line in lines:
        variant = line.get("variant")
        if variant is not None:
            held = need.setdefault(variant.pk, [variant, 0])
            held[1] += _units(line)
    short = []
    for variant, wanted in need.values():
        variant.refresh_from_db(fields=["stock", "reserved"])
        available = variant.available_stock
        if warehouse is not None:
            from apps.orders.variant_stock import at
            available = min(available, at(variant, warehouse))
        if available < wanted:
            short.append((variant, wanted, available))
    return short


def _check_stock(store, lines, actor=None, warehouse=None):
    """Refuse an invoice the shelf cannot cover, unless the store allows stok minus and ``actor`` may
    use it. Returns True when the invoice will sell short. With ``warehouse`` only that location's
    shelf is looked at."""
    from apps.orders.deficits import may_sell_short
    from apps.orders.inventory import stock_shortages
    where = f" di {warehouse.name}" if warehouse is not None else ""
    sizes = _variant_shortages(lines, warehouse)
    if sizes:  # a variant is never sold short (stok minus is for products without variants)
        raise StockShortage("Stok tidak cukup: " + "; ".join(f"{variant} ({variant.sku}) butuh {wanted}, "
            f"tersedia {available}{where}" for variant, wanted, available in sizes)
            + ". Tambah stok varian ini lebih dulu.")
    short = stock_shortages(store, _wants(lines), warehouse)
    if not short:
        return False
    detail = "; ".join(f"{product.name} ({product.sku}) butuh {wanted}, tersedia {available}{where}"
        for product, wanted, available in short)
    if not store.allow_negative_stock:
        raise StockShortage("Stok tidak cukup: " + detail
            + ". Tambah stok lebih dulu (terima barang dari tagihan supplier atau stok opname).")
    if not may_sell_short(store, actor):
        raise StockShortage("Stok tidak cukup: " + detail
            + ". Menjual melebihi stok (stok minus) hanya boleh dilakukan admin atau manajer.")
    no_cost = [product.name for product, _w, _a in short if product.cost_price is None]
    if no_cost:
        raise StockShortage("Stok tidak cukup dan harga pokok belum diisi: " + ", ".join(no_cost)
            + ". Isi harga pokoknya dulu; sistem tidak menebak harga pokok.")
    return True


def _build_order(store, *, lines, buyer_name, issued_on, channel, customer, phone, address, notes,
        external_order_id, discount, shipping, status, subtotal, tax, total, order_type="direct",
        courier="", tracking_number="", partner=None):
    extra = {"courier_code": (courier or "").strip().lower()[:40],
        "tracking_number": (tracking_number or "").strip()[:100]}
    if partner is not None:
        # A sale to a mitra (e.g. AB Shop): billed on this invoice at the mitra's prices, so it is
        # not offered again when the next settlement is made.
        extra.update(consignment_partner=partner, abshop_billing_status="billed")
        channel, order_type = "abshop", "reseller_sell_in"
    order = Order.objects.create(store=store, channel=channel, order_type=order_type, **extra,
        customer_name=buyer_name[:120], phone=(phone or "-")[:30],
        shipping_address=(address or "-")[:300], subtotal=subtotal, discount=discount, tax=tax,
        shipping_cost=shipping, total=total, payment_method="manual", delivery_notes=notes[:300],
        external_order_id=external_order_id[:100], customer=customer, idempotency_key=uuid.uuid4().hex)
    Order.objects.filter(pk=order.pk).update(created_at=timezone.make_aware(
        timezone.datetime.combine(issued_on, timezone.datetime.min.time().replace(hour=12))))
    order.refresh_from_db()
    order.save()  # generates the order's number
    for line in lines:
        product = line.get("product")
        if product:
            variant, unit_factor = line.get("variant"), int(line.get("unit_factor") or 1)
            # the shelf counts base units; the price follows
            from apps.catalog.units import per_base
            OrderItem.objects.create(order=order, product=product, variant=variant, name=product.name,
                sku=variant.sku if variant else product.sku, variant_name=variant.name if variant else "",
                unit_price=per_base(line["unit_price"], unit_factor), quantity=_units(line), line_total=line_net(line))
    OrderStatusEvent.objects.create(order=order, status=status,
        note=f"Invoice dibuat manual{' (belum dibayar)' if status == 'pending' else ''} · "
             f"channel: {channel}")
    order.status = status
    order.save(update_fields=["status"])
    return order


def _deduct(order, *, allow_negative=False, actor=None, warehouse=None):
    from apps.orders.inventory import deduct_invoice_stock
    if deduct_invoice_stock(order, allow_negative=allow_negative, actor=actor, warehouse=warehouse):  # someone else took the stock since the check
        raise StockShortage("Stok baru saja berkurang karena penjualan lain. Coba lagi.")


def _buyer_label(buyer_name, company_name):
    company_name = (company_name or "").strip()
    if not company_name:
        return buyer_name
    if buyer_name and buyer_name.strip().lower() != company_name.lower():
        return f"{company_name} ({buyer_name})"
    return company_name


def _write_items(invoice, lines):
    invoice.items.all().delete()
    for line in lines:
        product, variant = line.get("product"), line.get("variant")
        sku = variant.sku if variant else (product.sku if product else line.get("code", ""))
        InvoiceItem.objects.create(invoice=invoice, product=product, variant=variant,
            description=line["description"], sku=sku[:60],
            quantity=line["qty"], unit_price=line["unit_price"], discount=line["discount"],
            unit=line.get("unit", ""), unit_factor=int(line.get("unit_factor") or 1), note=line.get("note", ""),
            line_total=line_net(line))


@transaction.atomic
def create_sales_invoice(store, *, lines, buyer_name, issued_on=None, channel="manual",
        customer=None, company_name="", buyer_npwp="", phone="-", address="-", notes="",
        external_order_id="", discount=0, shipping=0, on_credit=False, due_date=None, actor=None,
        courier="", tracking_number="", partner=None, delivered_costs=None, sales_order=None,
        warehouse=None, number_series=None, rounding=0, cost_center=None, credit_check=False,
        prices_include_tax=False):
    """Create the order, take its stock, and issue the invoice. Returns the invoice. Stock leaves
    the shelf with the invoice and the cost of those goods is booked with it, so the two cannot
    disagree; a shortage refuses the invoice (``StockShortage``).

    ``warehouse`` sells from that location only (an outlet or the register's branch): what another
    branch holds does not count, and stok minus, when allowed, is recorded at that location.
    Without it the store's locations are drawn in priority order, as before. ``number_series``
    ("POS") numbers the invoice from its own sequence instead of the INV series. ``rounding``
    (signed) is added to the total and booked to Pembulatan penjualan (4950): a register rounds what
    the buyer pays to whole rupiah. ``cost_center`` is the outlet the sale counts for.

    ``credit_check`` refuses a credit sale that takes the customer past ``Customer.credit_limit``
    (``tax/credit_limit.py``); only the invoice form asks for it, automatic flows never do.

    ``delivered_costs`` (``{product id: unit cost}``) means the goods already left on a surat jalan
    (tax/sales_orders.py): no stock is taken, the costs given are the HPP, and the invoice is marked
    ``from_deliveries`` so posting credits 1410 instead of Persediaan."""
    lines = clean_lines(lines)
    from apps.orders.stock import StockError, check_warehouse
    try:
        check_warehouse(store, warehouse)
    except StockError as exc:
        raise InvoiceError(str(exc)) from exc
    short = False if delivered_costs is not None else _check_stock(store, lines, actor, warehouse)
    issued_on = issued_on or timezone.localdate()
    discount, shipping = Decimal(discount or 0), Decimal(shipping or 0)
    subtotal, tax, total = price_lines(lines, discount, shipping, store=store)
    rounding = Decimal(rounding or 0)
    if abs(rounding) >= 1000:
        raise InvoiceError("Pembulatan terlalu besar.")
    total += rounding
    if credit_check and on_credit:
        from .credit_limit import check
        check(customer, total)
    order = _build_order(store, lines=lines, buyer_name=buyer_name, issued_on=issued_on,
        channel=channel, customer=customer, phone=phone, address=address, notes=notes,
        external_order_id=external_order_id, discount=discount, shipping=shipping,
        status="pending" if on_credit else "paid", subtotal=subtotal, tax=tax, total=total,
        courier=courier, tracking_number=tracking_number, partner=partner)
    if delivered_costs is None:
        _deduct(order, allow_negative=short, actor=actor, warehouse=warehouse)
    else:
        for item in order.items.all():
            item.unit_cost = delivered_costs.get((item.product_id, item.variant_id),
                delivered_costs.get(item.product_id))
            item.save(update_fields=["unit_cost"])
    invoice, _ = issue_invoice_for_order(order, issued_on=issued_on, notes=notes, number_series=number_series,
        rounding=rounding, cost_center=cost_center)
    _write_items(invoice, lines)  # keeps services and per-line discounts; the order has only goods
    patch = {"buyer_name": _buyer_label(buyer_name, company_name), "warehouse": warehouse,
        "prices_include_tax": bool(prices_include_tax)}
    if delivered_costs is not None:
        patch.update(from_deliveries=True, sales_order=sales_order)
    if buyer_npwp:
        patch["buyer_npwp"] = buyer_npwp[:16]
    if on_credit and due_date:
        patch["due_date"] = due_date
    Invoice.objects.filter(pk=invoice.pk).update(**patch)
    invoice.refresh_from_db()
    if delivered_costs is not None:
        from .posting import repost_invoice
        repost_invoice(invoice, actor=actor)  # it was booked before it was flagged: book it against 1410
    return invoice


# ---------------------------------------------------------------------------
# Editing an issued invoice
# ---------------------------------------------------------------------------

def invoice_locks(invoice):
    """Why the amounts of an invoice can no longer be edited (empty: they can). Money that has
    moved or a document built on it is corrected with a credit note, never by rewriting."""
    from .posting import SETTLED_ON_ISSUE
    order, reasons = invoice.order, []
    if invoice.status == Invoice.STATUS_VOID:
        return ["invoice sudah dibatalkan"]
    if any(p.note != SETTLED_ON_ISSUE for p in invoice.payments.all()):
        reasons.append("sudah ada pembayaran")
    if invoice.credit_notes.exclude(status="void").exists():
        reasons.append("sudah ada nota kredit")
    if order.payment_transactions.filter(kind="payment", status="succeeded").exists():
        reasons.append("dibayar lewat gateway atau marketplace")
    if getattr(order, "channel_order", None):
        reasons.append("diimpor dari marketplace")
    if order.order_type == "opening":
        reasons.append("saldo awal dari pembukuan lama")
    elif order.order_type != "direct":
        reasons.append("pesanan mitra")
    if invoice.from_deliveries:
        reasons.append("dibuat dari surat jalan")
    return reasons


def _signature(lines):
    return sorted((line["product"].pk if line.get("product") else 0,
        line["variant"].pk if line.get("variant") else 0, int(line.get("unit_factor") or 1), line["description"],
        Decimal(line["qty"]).normalize(), Decimal(line["unit_price"]).normalize(),
        Decimal(line.get("discount") or 0).normalize()) for line in lines)


def invoice_lines(invoice):
    """An invoice's lines in the form the editor and the services use."""
    return [{"product": item.product, "variant": item.variant, "description": item.description, "qty": item.quantity,
        "unit_price": item.unit_price, "discount": item.discount, "unit": item.unit, "unit_factor": item.unit_factor,
        "note": item.note, "code": "" if item.product_id else item.sku}
        for item in invoice.items.select_related("product", "variant")]


@transaction.atomic
def update_sales_invoice(invoice, *, buyer_name, issued_on, due_date, notes, company_name="",
        buyer_npwp="", buyer_address="", phone="-", customer=None, external_order_id="",
        lines=None, discount=None, shipping=None, actor=None, credit_check=False, rounding=None,
        prices_include_tax=None):
    """Change an invoice. Buyer, dates and notes can always change; lines and amounts only while
    nothing was paid or credited on it. Changed goods give the old stock back, take the new,
    and rebook the sale and its cost.

    ``rounding`` is the leftover of prices re-typed with PPN in them (``from_inclusive``); lines priced
    before PPN come out exact and leave none. ``prices_include_tax`` records how the lines were typed."""
    from apps.accounting.ledger import reverse_source
    from apps.orders.inventory import return_order_inventory

    from .posting import SETTLED_ON_ISSUE, repost_invoice
    invoice = Invoice.objects.select_for_update().select_related("order").get(pk=invoice.pk)
    if invoice.status == Invoice.STATUS_VOID:
        raise InvoiceError("Invoice yang dibatalkan tidak bisa diubah.")
    order = invoice.order
    amounts_changed = False
    if lines is not None:
        lines = clean_lines(lines)
        discount = Decimal(discount if discount is not None else invoice.discount)
        shipping = Decimal(shipping if shipping is not None else invoice.shipping)
        same = (_signature(lines) == _signature(invoice_lines(invoice))
            and discount == invoice.discount and shipping == invoice.shipping)
        if not same:
            locks = invoice_locks(invoice)
            if locks:
                raise InvoiceError("Jumlah invoice tidak bisa diubah karena " + ", ".join(locks)
                    + ". Koreksi lewat nota kredit.")
            amounts_changed = True
    date_changed = issued_on != invoice.issued_on
    settled = [p for p in invoice.payments.all() if p.note == SETTLED_ON_ISSUE]

    if lines is not None and not amounts_changed:
        _write_items(invoice, lines)  # unit, note and code are only presentation
    if amounts_changed:
        memo = f"Edit invoice {invoice.number}"
        return_order_inventory(order, actor=actor, reason=memo)  # the old goods come back first
        warehouse = invoice.warehouse  # sold from one location: the edit takes the new goods from there too
        short = _check_stock(invoice.store, lines, actor, warehouse)  # sees what the edit can use
        subtotal, tax, total = price_lines(lines, discount, shipping, store=invoice.store,
            is_pkp=invoice.seller_is_pkp)
        rounding = Decimal(rounding or 0)
        if abs(rounding) >= 1000:
            raise InvoiceError("Pembulatan terlalu besar.")
        total += rounding
        if credit_check and not settled:  # a credit invoice raised: the same limit as a new one
            from .credit_limit import check
            check(customer or order.customer, total, exclude=invoice)
        new_order = _build_order(invoice.store, lines=lines, buyer_name=buyer_name,
            issued_on=issued_on, channel=order.channel, customer=customer or order.customer,
            phone=phone, address=buyer_address, notes=notes,
            external_order_id=external_order_id or order.external_order_id, discount=discount,
            shipping=shipping, status=order.status if order.status in {"pending", "paid"} else "paid",
            subtotal=subtotal, tax=tax, total=total, partner=order.consignment_partner)
        _deduct(new_order, allow_negative=short, actor=actor, warehouse=warehouse)
        Order.objects.filter(pk=order.pk).update(status="cancelled")  # no signals for a swap
        OrderStatusEvent.objects.create(order=order, status="cancelled",
            note=f"Diganti pesanan {new_order.invoice_number} karena invoice {invoice.number} diedit")
        invoice.order = new_order
        invoice.subtotal, invoice.discount, invoice.shipping = subtotal, discount, shipping
        invoice.tax_base = max(subtotal - discount, ZERO)
        invoice.tax_rate = vat_for(invoice.store, invoice.tax_base, is_pkp=invoice.seller_is_pkp)[0]
        # Only the rounding of the new lines is kept: an earlier one would leave Pembulatan holding an
        # amount the new total no longer has.
        invoice.tax, invoice.total, invoice.other_charges, invoice.rounding = tax, total, ZERO, rounding
        _write_items(invoice, lines)
        order = new_order
    else:
        Order.objects.filter(pk=order.pk).update(customer_name=buyer_name[:120],
            phone=(phone or "-")[:30], shipping_address=(buyer_address or "-")[:300],
            delivery_notes=notes[:300], external_order_id=external_order_id[:100],
            customer=customer or order.customer)

    if amounts_changed and prices_include_tax is not None:
        invoice.prices_include_tax = bool(prices_include_tax)
    invoice.issued_on, invoice.due_date, invoice.notes = issued_on, due_date, notes[:500]
    invoice.buyer_name = _buyer_label(buyer_name, company_name)
    invoice.buyer_address, invoice.buyer_npwp = buyer_address[:300], buyer_npwp[:16]
    invoice.save()
    if amounts_changed or date_changed:
        for payment in settled:  # a cash sale's automatic payment follows the invoice
            reverse_source(invoice.store, "invoice_payment", payment.pk, actor=actor,
                memo=f"Koreksi invoice {invoice.number}")
            payment.amount, payment.paid_on = invoice.total, issued_on
            payment.save(update_fields=["amount", "paid_on"])
        repost_invoice(invoice, actor=actor)
    return invoice
