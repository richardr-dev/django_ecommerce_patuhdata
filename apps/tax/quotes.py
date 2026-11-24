"""Penawaran (quotes): a price offered before the sale. A quote books nothing; when the buyer
agrees it becomes a sales invoice with the same lines, prices and buyer."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Invoice, Quote, QuoteItem
from .numbering import next_number
from .sales import create_sales_invoice, price_lines
from .services import InvoiceError


class QuoteError(ValueError):
    pass


@transaction.atomic
def save_quote(store, *, lines, buyer_name, issued_on=None, valid_days=14, valid_until=None,
        customer=None, company_name="", buyer_npwp="", phone="", address="", notes="",
        discount=0, shipping=0, quote=None, actor=None):
    """Create a quote, or replace the lines and details of a draft/sent one."""
    if not lines:
        raise QuoteError("Tambahkan minimal satu produk.")
    if not buyer_name.strip():
        raise QuoteError("Nama pelanggan wajib diisi.")
    issued_on = issued_on or timezone.localdate()
    valid_until = valid_until or issued_on + timedelta(days=valid_days)
    subtotal, tax, total = price_lines(lines, discount, shipping, store=store)
    fields = {"customer": customer, "buyer_name": buyer_name.strip()[:160],
        "company_name": company_name[:160], "buyer_npwp": buyer_npwp[:16], "phone": phone[:30],
        "address": address, "notes": notes[:500], "discount": Decimal(discount or 0),
        "shipping": Decimal(shipping or 0), "subtotal": subtotal, "tax": tax, "total": total,
        "issued_on": issued_on, "valid_until": valid_until}
    if quote is None:
        quote = Quote.objects.create(store=store, number=next_number(store, "QT", issued_on),
            created_by=actor, **fields)
    else:
        quote = Quote.objects.select_for_update().get(pk=quote.pk)
        if quote.status in {Quote.STATUS_INVOICED}:
            raise QuoteError("Penawaran ini sudah dijadikan invoice.")
        for key, value in fields.items():
            setattr(quote, key, value)
        quote.save()
        quote.items.all().delete()
    for line in lines:
        product, variant = line["product"], line.get("variant")
        QuoteItem.objects.create(quote=quote, product=product, variant=variant,
            description=f"{product.name} / {variant.name}" if variant else product.name,
            sku=variant.sku if variant else product.sku, quantity=line["qty"], unit_price=line["unit_price"],
            line_total=Decimal(line["unit_price"]) * Decimal(line["qty"]))
    return quote


def set_status(quote, status):
    if status not in {Quote.STATUS_SENT, Quote.STATUS_ACCEPTED, Quote.STATUS_DECLINED,
            Quote.STATUS_DRAFT}:
        raise QuoteError("Status tidak dikenal.")
    if quote.status == Quote.STATUS_INVOICED:
        raise QuoteError("Penawaran ini sudah dijadikan invoice.")
    quote.status = status
    quote.save(update_fields=["status"])
    return quote


def is_expired(quote, today=None):
    today = today or timezone.localdate()
    return bool(quote.valid_until and quote.valid_until < today
        and quote.status in {Quote.STATUS_DRAFT, Quote.STATUS_SENT})


@transaction.atomic
def convert_to_invoice(quote, *, issued_on=None, on_credit=True, due_days=14, actor=None):
    quote = Quote.objects.select_for_update().get(pk=quote.pk)
    if quote.status == Quote.STATUS_INVOICED or quote.invoice_id:
        raise QuoteError("Penawaran ini sudah dijadikan invoice.")
    if quote.status == Quote.STATUS_DECLINED:
        raise QuoteError("Penawaran yang ditolak tidak bisa dijadikan invoice.")
    issued_on = issued_on or timezone.localdate()
    lines = [{"product": item.product, "variant": item.variant, "qty": item.quantity, "unit_price": item.unit_price}
        for item in quote.items.select_related("product", "variant")]
    try:
        invoice = create_sales_invoice(quote.store, lines=lines, buyer_name=quote.buyer_name,
            issued_on=issued_on, customer=quote.customer, company_name=quote.company_name,
            buyer_npwp=quote.buyer_npwp, phone=quote.phone or "-", address=quote.address or "-",
            notes=quote.notes or f"Berdasarkan penawaran {quote.number}", discount=quote.discount,
            shipping=quote.shipping, on_credit=on_credit,
            due_date=issued_on + timedelta(days=due_days), actor=actor)
    except InvoiceError as error:
        raise QuoteError(str(error)) from error
    quote.invoice = Invoice.objects.get(pk=invoice.pk)
    quote.status = Quote.STATUS_INVOICED
    quote.save(update_fields=["invoice", "status"])
    return invoice
