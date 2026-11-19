"""Invoice gabungan: several issued invoices printed as one bill.

The separate invoices stay what they are (each one is booked, numbered and paid on its own); this is an
extra document on top, for a buyer who wants one bill for a period or a batch of orders. It lists every
product over all the chosen invoices with its combined quantity and amount, then each invoice with its
own lines and subtotal, then one set of totals and what is still owed. It books nothing."""
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal

from .models import Invoice

ZERO = Decimal(0)
MAX_INVOICES = 200


class CombinedInvoiceError(Exception):
    pass


@dataclass
class CombinedInvoice:
    invoices: list
    products: list
    totals: dict
    buyer_name: str
    buyer_address: str
    buyer_npwp: str
    one_buyer: bool
    first_date: object
    last_date: object
    number: str
    left_out: list = field(default_factory=list)


def combine(store, invoices):
    """Build the combined bill from ``invoices`` (any iterable of this store's invoices). Voided and
    draft invoices are left out and listed in ``left_out``; nothing left raises CombinedInvoiceError."""
    chosen, left_out = [], []
    for invoice in invoices:
        if invoice.store_id != store.pk:
            continue
        if invoice.status in (Invoice.STATUS_VOID, Invoice.STATUS_DRAFT):
            left_out.append(invoice)
        else:
            chosen.append(invoice)
    if not chosen:
        raise CombinedInvoiceError("Pilih minimal satu invoice yang sudah terbit dan tidak dibatalkan.")
    if len(chosen) > MAX_INVOICES:
        raise CombinedInvoiceError(f"Invoice gabungan paling banyak {MAX_INVOICES} invoice sekaligus.")
    chosen.sort(key=lambda inv: (inv.issued_on, inv.number))

    products = OrderedDict()
    for invoice in chosen:
        invoice.lines = list(invoice.items.all())
        for item in invoice.lines:
            # One row per product and price; a line without a product groups by its code and text.
            key = (item.product_id or item.sku or item.description, item.unit, item.unit_price)
            row = products.get(key)
            if row is None:
                row = products[key] = {"description": item.description, "sku": item.sku, "unit": item.unit,
                    "unit_price": item.unit_price, "quantity": ZERO, "discount": ZERO, "amount": ZERO,
                    "invoices": 0}
            row["quantity"] += item.quantity
            row["discount"] += item.discount
            row["amount"] += item.line_total
            row["invoices"] += 1

    def total(name):
        return sum((getattr(inv, name) for inv in chosen), ZERO)

    paid = sum((inv.amount_paid for inv in chosen), ZERO)
    credited = sum((inv.credited for inv in chosen), ZERO)
    totals = {"subtotal": total("subtotal"), "discount": total("discount"), "shipping": total("shipping"),
        "other_charges": total("other_charges"), "tax": total("tax"), "rounding": total("rounding"),
        "total": total("total"), "paid": paid, "credited": credited,
        "due": sum((inv.amount_due for inv in chosen), ZERO),
        "quantity": sum((row["quantity"] for row in products.values()), ZERO)}

    buyers = {(inv.buyer_name or "").strip().lower() for inv in chosen}
    one_buyer = len(buyers) == 1
    first = chosen[0]
    first_date, last_date = chosen[0].issued_on, chosen[-1].issued_on
    # A stable reference for the document itself (it is not an invoice number of the INV series).
    number = f"GAB-{last_date:%Y%m%d}-{first.number.rsplit('/', 1)[-1]}-{len(chosen)}"
    return CombinedInvoice(invoices=chosen, products=list(products.values()), totals=totals,
        buyer_name=first.buyer_name if one_buyer else "Beberapa pelanggan",
        buyer_address=first.buyer_address if one_buyer else "",
        buyer_npwp=first.buyer_npwp if one_buyer else "",
        one_buyer=one_buyer, first_date=first_date, last_date=last_date, number=number, left_out=left_out)
