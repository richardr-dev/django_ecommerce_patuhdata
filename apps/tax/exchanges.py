"""Tukar barang: swap goods on an issued invoice (another size, another item) in one step.

The original invoice is never edited or struck through: it stays as the buyer received it. Instead, in one
transaction:

1. a credit note takes the exchanged line back (restocked when the goods came back in good condition; a
   damaged unit stays out and its cost remains in HPP);
2. a replacement invoice sells the new item through ``create_sales_invoice`` (stock out, HPP, same buyer,
   channel, mitra and location as the original);
3. whatever the credit note could not take off the original (it was already paid) became customer credit,
   and that credit pays the replacement invoice as far as it reaches.

So the same price means nothing new to collect; a dearer replacement leaves the difference open on the
replacement invoice; a cheaper one leaves customer credit (to use later or refund). An ``InvoiceExchange``
row ties the three documents so each shows the others."""
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import LedgerError
from apps.catalog.models import ProductVariant

from .credit_notes import CreditNoteError, create_credit_note, credited_quantity
from .models import CreditNote, CustomerCredit, Invoice, InvoiceExchange, InvoiceItem
from .receipts import ReceiptError, apply_credit
from .sales import create_sales_invoice
from .services import InvoiceError

ZERO = Decimal(0)


class ExchangeError(ValueError):
    pass


def find_replacement(store, code):
    """``(product, variant)`` for a SKU, a variant SKU or a variant barcode; raises ExchangeError."""
    from .invoice_batch import _products
    found = _products(store).get((code or "").strip().lower())
    if found is None:
        raise ExchangeError(f"SKU “{code}” tidak ditemukan di Produk.")
    if isinstance(found, ProductVariant):
        return found.product, found
    if found.variants.filter(is_active=True).exists():
        raise ExchangeError(f"{found.sku} punya varian: tulis SKU varian ukurannya.")
    return found, None


def exchangeable_quantity(item):
    """Units of this line that can still be swapped (not yet credited back)."""
    return item.quantity - credited_quantity(item)


def _net_unit(item):
    """What the buyer was charged per unit of the line, after its own discount."""
    return ((item.quantity * item.unit_price - (item.discount or ZERO)) / item.quantity).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN)


@transaction.atomic
def exchange_line(invoice, *, item, quantity, replacement_code, condition="good", unit_price=None,
        issued_on=None, reason="", actor=None):
    """Swap ``quantity`` of ``item`` for the product named by ``replacement_code``. Returns the
    ``InvoiceExchange``. ``unit_price`` (net, per unit) prices the replacement; empty keeps what the buyer paid
    for the original. Raises ExchangeError with a message for the screen."""
    invoice = Invoice.objects.select_for_update().select_related("order", "store").get(pk=invoice.pk)
    store = invoice.store
    if invoice.status in {Invoice.STATUS_VOID, Invoice.STATUS_DRAFT}:
        raise ExchangeError("Invoice ini tidak bisa ditukar barangnya.")
    if invoice.order.order_type == "opening":
        raise ExchangeError("Invoice saldo awal tidak memakai penukaran barang.")
    item = InvoiceItem.objects.filter(pk=getattr(item, "pk", item), invoice=invoice).select_related(
        "product", "variant").first()
    if item is None or item.product_id is None:
        raise ExchangeError("Pilih baris barang yang ditukar.")
    if item.unit_factor != 1:
        raise ExchangeError("Baris yang dijual per satuan besar (dus) ditukar lewat nota kredit dan invoice baru.")
    try:
        quantity = Decimal(str(quantity))
    except ArithmeticError:
        raise ExchangeError("Jumlah tidak terbaca.") from None
    left = exchangeable_quantity(item)
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > left:
        raise ExchangeError(f"Jumlah yang bisa ditukar 1 sampai {left:g}.")
    if condition not in dict(InvoiceExchange.CONDITIONS):
        raise ExchangeError("Pilih kondisi barang yang kembali.")
    product, variant = find_replacement(store, replacement_code)
    if product.pk == item.product_id and (variant.pk if variant else None) == item.variant_id:
        raise ExchangeError("Barang pengganti sama dengan barang yang ditukar.")
    issued_on = issued_on or timezone.localdate()
    if unit_price in (None, ""):
        price, discount = item.unit_price, ((item.discount or ZERO) * quantity / item.quantity).quantize(
            Decimal("0.01"))
    else:
        price, discount = Decimal(str(unit_price)), ZERO
        if price < 0:
            raise ExchangeError("Harga pengganti tidak boleh negatif.")
    order = invoice.order
    label = variant.sku if variant else product.sku
    try:
        note = create_credit_note(invoice, issued_on=issued_on, reason="return", actor=actor,
            warehouse=invoice.warehouse,
            notes=f"Penukaran barang: {item.sku or item.description} ditukar dengan {label}",
            lines=[{"item": item, "quantity": quantity, "unit_price": _net_unit(item),
                "restock": condition == "good"}])
        replacement = create_sales_invoice(store, lines=[{"product": product, "variant": variant, "qty": quantity,
                "unit_price": price, "discount": discount}],
            buyer_name=invoice.buyer_name, customer=order.customer, buyer_npwp=invoice.buyer_npwp,
            phone=order.phone or "-", address=invoice.buyer_address or order.shipping_address or "-",
            channel=order.channel, issued_on=issued_on, on_credit=True, due_date=invoice.due_date
            if invoice.due_date and invoice.due_date >= issued_on else None,
            external_order_id=order.external_order_id, partner=order.consignment_partner,
            warehouse=invoice.warehouse, actor=actor,
            notes=f"Pengganti dari {invoice.number} (penukaran barang).")
        credit = CustomerCredit.objects.filter(credit_note=note, kind="credit_note").first()
        if credit is not None and credit.amount > 0:
            replacement.refresh_from_db()
            use = min(credit.amount, replacement.amount_due)
            if use > 0:
                apply_credit(replacement, use, issued_on, actor=actor)
    except (CreditNoteError, InvoiceError, ReceiptError, LedgerError) as error:
        raise ExchangeError(str(error)) from error
    exchange = InvoiceExchange.objects.create(store=store, original_invoice=invoice, original_item=item,
        quantity=quantity, condition=condition, credit_note=note, replacement_invoice=replacement,
        replacement_product=product, replacement_variant=variant, replacement_sku=label,
        reason=reason[:200], created_by=actor)
    from apps.shops.models import AuditLog
    AuditLog.objects.create(store=store, actor=actor, action="invoice.exchanged", object_type="Invoice",
        object_id=str(invoice.pk), details={"number": invoice.number, "from": item.sku, "to": label,
            "quantity": str(quantity), "credit_note": note.number, "replacement": replacement.number})
    return exchange


def exchange_state(invoice):
    """"Ditukar" when every goods line was swapped in full, "Sebagian ditukar" when some was, else ""."""
    swaps = list(invoice.exchanges_out.all())
    if not swaps:
        return ""
    swapped = {}
    for swap in swaps:
        swapped[swap.original_item_id] = swapped.get(swap.original_item_id, ZERO) + swap.quantity
    goods = [item for item in invoice.items.all() if item.product_id]
    whole = goods and all(swapped.get(item.pk, ZERO) >= item.quantity for item in goods)
    return "Ditukar" if whole else "Sebagian ditukar"


def credit_notes_without_exchange(invoice):
    return [note for note in invoice.credit_notes.all() if note.status != CreditNote.STATUS_VOID
        and not hasattr(note, "exchange")]


def exchange_context(store, invoice, items):
    """What the invoice page needs: the lines that can still be swapped, the SKUs on offer (with stock), the
    swaps made from this invoice (per line) and the one it replaces."""
    from apps.catalog.models import Product
    swaps = list(invoice.exchanges_out.select_related("replacement_invoice", "credit_note", "original_item"))
    by_item = {}
    for swap in swaps:
        by_item.setdefault(swap.original_item_id, []).append(swap)
    lines = []
    for item in items:
        item.exchange_rows = by_item.get(item.pk, [])
        if item.product_id and item.unit_factor == 1:
            left = exchangeable_quantity(item)
            if left > 0:
                lines.append({"item": item, "left": int(left)})
    offer = []
    if lines:
        for product in Product.objects.filter(store=store, is_active=True).exclude(sku="").prefetch_related(
                "variants", "inventory_levels").order_by("name")[:3000]:
            variants = [v for v in product.variants.all() if v.is_active]
            if variants:
                offer += [(v.sku, f"{product.name} {v.name}", v.stock - v.reserved) for v in variants if v.sku]
            else:
                offer.append((product.sku, product.name, sum(level.on_hand - level.reserved
                    for level in product.inventory_levels.all())))
    replaces = getattr(invoice, "exchange_in", None) if hasattr(invoice, "exchange_in") else None
    return {"lines": lines, "offer": offer, "swaps": swaps, "by_item": by_item, "replaces": replaces,
        "state": exchange_state(invoice) if swaps else ""}
