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
from .services import InvoiceError, issue_invoice_for_order, vat_for

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


def clean_lines(lines):
    """Drop empty rows and refuse a half-filled one."""
    kept = []
    for line in lines:
        product = line.get("product")
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
        kept.append({"product": product, "description": text or product.name, "qty": qty,
            "unit_price": price, "discount": discount, "unit": (line.get("unit") or "").strip()[:20],
            "note": (line.get("note") or "").strip()[:300],
            "code": (line.get("code") or "").strip()[:60]})
    if not kept:
        raise InvoiceError("Tambahkan minimal satu barang atau jasa.")
    return kept


def _wants(lines):
    return [(line["product"], int(line["qty"])) for line in lines if line.get("product")]


def _check_stock(store, lines):
    from apps.orders.inventory import stock_shortages
    short = stock_shortages(store, _wants(lines))
    if short:
        raise StockShortage("Stok tidak cukup: " + "; ".join(
            f"{product.name} ({product.sku}) butuh {wanted}, tersedia {available}"
            for product, wanted, available in short)
            + ". Tambah stok lebih dulu (terima barang dari tagihan supplier atau stok opname).")


def _build_order(store, *, lines, buyer_name, issued_on, channel, customer, phone, address, notes,
        external_order_id, discount, shipping, status, subtotal, tax, total, order_type="direct"):
    order = Order.objects.create(store=store, channel=channel, order_type=order_type,
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
            OrderItem.objects.create(order=order, product=product, name=product.name,
                sku=product.sku, unit_price=line["unit_price"], quantity=int(line["qty"]),
                line_total=line_net(line))
    OrderStatusEvent.objects.create(order=order, status=status,
        note=f"Invoice dibuat manual{' (belum dibayar)' if status == 'pending' else ''} · "
             f"channel: {channel}")
    order.status = status
    order.save(update_fields=["status"])
    return order


def _deduct(order):
    from apps.orders.inventory import deduct_invoice_stock
    if deduct_invoice_stock(order):  # someone else took the stock since the check
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
        product = line.get("product")
        InvoiceItem.objects.create(invoice=invoice, product=product,
            description=line["description"], sku=product.sku if product else line.get("code", ""),
            quantity=line["qty"], unit_price=line["unit_price"], discount=line["discount"],
            unit=line.get("unit", ""), note=line.get("note", ""), line_total=line_net(line))


@transaction.atomic
def create_sales_invoice(store, *, lines, buyer_name, issued_on=None, channel="manual",
        customer=None, company_name="", buyer_npwp="", phone="-", address="-", notes="",
        external_order_id="", discount=0, shipping=0, on_credit=False, due_date=None, actor=None):
    """Create the order, take its stock, and issue the invoice. Returns the invoice. Stock leaves
    the shelf with the invoice and the cost of those goods is booked with it, so the two cannot
    disagree; a shortage refuses the invoice (``StockShortage``)."""
    lines = clean_lines(lines)
    _check_stock(store, lines)
    issued_on = issued_on or timezone.localdate()
    discount, shipping = Decimal(discount or 0), Decimal(shipping or 0)
    subtotal, tax, total = price_lines(lines, discount, shipping, store=store)
    order = _build_order(store, lines=lines, buyer_name=buyer_name, issued_on=issued_on,
        channel=channel, customer=customer, phone=phone, address=address, notes=notes,
        external_order_id=external_order_id, discount=discount, shipping=shipping,
        status="pending" if on_credit else "paid", subtotal=subtotal, tax=tax, total=total)
    _deduct(order)
    invoice, _ = issue_invoice_for_order(order, issued_on=issued_on, notes=notes)
    _write_items(invoice, lines)  # keeps services and per-line discounts; the order has only goods
    patch = {"buyer_name": _buyer_label(buyer_name, company_name)}
    if buyer_npwp:
        patch["buyer_npwp"] = buyer_npwp[:16]
    if on_credit and due_date:
        patch["due_date"] = due_date
    Invoice.objects.filter(pk=invoice.pk).update(**patch)
    invoice.refresh_from_db()
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
    if order.order_type != "direct":
        reasons.append("pesanan mitra")
    return reasons


def _signature(lines):
    return sorted((line["product"].pk if line.get("product") else 0, line["description"],
        Decimal(line["qty"]).normalize(), Decimal(line["unit_price"]).normalize(),
        Decimal(line.get("discount") or 0).normalize()) for line in lines)


def invoice_lines(invoice):
    """An invoice's lines in the form the editor and the services use."""
    return [{"product": item.product, "description": item.description, "qty": item.quantity,
        "unit_price": item.unit_price, "discount": item.discount, "unit": item.unit,
        "note": item.note, "code": "" if item.product_id else item.sku}
        for item in invoice.items.all()]


@transaction.atomic
def update_sales_invoice(invoice, *, buyer_name, issued_on, due_date, notes, company_name="",
        buyer_npwp="", buyer_address="", phone="-", customer=None, external_order_id="",
        lines=None, discount=None, shipping=None, actor=None):
    """Change an invoice. Buyer, dates and notes can always change; lines and amounts only while
    nothing was paid or credited on it. Changed goods give the old stock back, take the new,
    and rebook the sale and its cost."""
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
        _check_stock(invoice.store, lines)  # so the check sees what the edit can use
        subtotal, tax, total = price_lines(lines, discount, shipping, store=invoice.store,
            is_pkp=invoice.seller_is_pkp)
        new_order = _build_order(invoice.store, lines=lines, buyer_name=buyer_name,
            issued_on=issued_on, channel=order.channel, customer=customer or order.customer,
            phone=phone, address=buyer_address, notes=notes,
            external_order_id=external_order_id or order.external_order_id, discount=discount,
            shipping=shipping, status=order.status if order.status in {"pending", "paid"} else "paid",
            subtotal=subtotal, tax=tax, total=total)
        _deduct(new_order)
        Order.objects.filter(pk=order.pk).update(status="cancelled")  # no signals for a swap
        OrderStatusEvent.objects.create(order=order, status="cancelled",
            note=f"Diganti pesanan {new_order.invoice_number} karena invoice {invoice.number} diedit")
        invoice.order = new_order
        invoice.subtotal, invoice.discount, invoice.shipping = subtotal, discount, shipping
        invoice.tax_base = max(subtotal - discount, ZERO)
        invoice.tax_rate = vat_for(invoice.store, invoice.tax_base, is_pkp=invoice.seller_is_pkp)[0]
        invoice.tax, invoice.total, invoice.other_charges = tax, total, ZERO
        _write_items(invoice, lines)
        order = new_order
    else:
        Order.objects.filter(pk=order.pk).update(customer_name=buyer_name[:120],
            phone=(phone or "-")[:30], shipping_address=(buyer_address or "-")[:300],
            delivery_notes=notes[:300], external_order_id=external_order_id[:100],
            customer=customer or order.customer)

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
