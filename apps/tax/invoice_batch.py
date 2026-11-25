"""Buat banyak invoice: many sales invoices in one go, every one through ``create_sales_invoice``, so they
price, tax, take stock and post exactly like an invoice made on the form.

Two ways in, one plan:

- **Paste from Excel**: one row per line, ``no. ref; pelanggan; tanggal; jatuh tempo; barang atau jasa; jumlah;
  harga; diskon`` (tab or ``;``). Rows that share a reference are one invoice; the reference is kept as the
  invoice's Referensi. "Barang atau jasa" is a product SKU (stock and HPP) or else the text of a service line.
  An empty price takes the customer's price list, else the product's price.
- **The same lines to many customers** (a monthly fee, a rent): the lines once, the customers ticked.

``plan`` reads the input and prices every invoice without writing anything, naming what is wrong per invoice,
so the page can show a preview. ``create_all`` makes every planned invoice or none (one transaction: an
invoice that fails rolls back the ones before it, and the gapless numbers with them)."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import LedgerError
from apps.accounting.opening_paste import parse_amount, parse_date
from apps.catalog.models import Product, ProductVariant
from apps.shops.models import Customer

from .pricing import customer_price, due_date_for
from .sales import clean_lines, create_sales_invoice, from_inclusive, price_lines
from .services import InvoiceError

ZERO = Decimal(0)
MAX_INVOICES = 300
HEADER_WORDS = {"no", "no.", "ref", "referensi", "no ref", "no. ref", "nomor", "kelompok"}
COLUMNS = ["No. ref", "Pelanggan", "Tanggal", "Jatuh tempo", "Barang (SKU) atau jasa", "Jumlah", "Harga",
    "Diskon", "Telepon", "Alamat", "Saluran"]
# Order.channel for the words a sales sheet uses ("Shopee Toko Mitra", "Tokopedia", "TikTok"...).
CHANNEL_WORDS = (("shopee", "shopee"), ("tokopedia", "tokopedia"), ("tiktok", "tiktok_shop"), ("lazada", "lazada"),
    ("pos", "pos"), ("kasir", "pos"), ("web", "web"), ("tv", "tv"))


def channel_for(text):
    text = (text or "").lower()
    return next((code for word, code in CHANNEL_WORDS if word in text), "manual")


class BatchError(InvoiceError):
    pass


@dataclass
class Planned:
    """One invoice to make. ``errors`` empty: it can be made."""
    key: str
    customer: Customer | None
    buyer_name: str
    issued_on: date
    due_date: date | None
    reference: str = ""
    lines: list = field(default_factory=list)
    rows: list = field(default_factory=list)       # source line numbers of a paste
    errors: list = field(default_factory=list)
    new_customer: bool = False
    subtotal: Decimal = ZERO
    tax: Decimal = ZERO
    total: Decimal = ZERO
    discount: Decimal = ZERO
    rounding: Decimal = ZERO
    inclusive: bool = False                        # prices typed with PPN in them
    phone: str = ""                                # optional: for a customer that is not in the CRM yet
    address: str = ""
    channel: str = "manual"

    @property
    def ok(self):
        return not self.errors


def find_customer(store, name):
    name = (name or "").strip()
    if not name:
        return None
    found = Customer.objects.filter(store=store)
    return (found.filter(name__iexact=name).first() or found.filter(company_name__iexact=name).first()
        or (found.filter(phone=name).first() if name.replace("+", "").isdigit() else None))


def _products(store):
    """``{sku: product or variant}``: a variant's SKU (or barcode) sells that size or colour."""
    found = {p.sku.lower(): p for p in Product.objects.filter(store=store, is_active=True).exclude(sku="")}
    for variant in ProductVariant.objects.filter(product__store=store, product__is_active=True,
            is_active=True).select_related("product"):
        for code in (variant.barcode, variant.sku):
            if code:
                found[code.lower()] = variant
    return found


def _line(text, qty, price, discount, *, products, customer):
    """One line from its typed cells: ``(line, error)``."""
    text = (text or "").strip()
    product, variant = products.get(text.lower()), None
    if isinstance(product, ProductVariant):
        product, variant = product.product, product
    elif product is not None and product.variants.filter(is_active=True).exists():
        return None, f"{product.sku} punya varian; tulis SKU variannya (ukuran/warna)"
    qty_value = parse_amount(qty) if (qty or "").strip() else Decimal(1)
    if qty_value is None or qty_value <= 0:
        return None, f"jumlah “{qty}” tidak terbaca"
    if (price or "").strip():
        unit_price = parse_amount(price)
        if unit_price is None:
            return None, f"harga “{price}” tidak terbaca"
    elif product is not None:
        listed = customer_price(customer, product, variant)   # None: the list says nothing, sell at retail
        unit_price = Decimal(listed if listed is not None else (variant.effective_price if variant else product.price))
    else:
        return None, f"“{text}” bukan SKU produk; isi harganya untuk baris jasa"
    line_discount = parse_amount(discount) if (discount or "").strip() else ZERO
    if line_discount is None:
        return None, f"diskon “{discount}” tidak terbaca"
    return {"product": product, "variant": variant, "description": "" if product else text, "qty": qty_value,
        "unit_price": unit_price, "discount": line_discount}, ""


def _price(store, planned, *, inclusive):
    """Check the lines and work out the totals the invoice will have."""
    try:
        lines = clean_lines(planned.lines)
    except InvoiceError as error:
        planned.errors.append(str(error))
        return
    discount, rounding = ZERO, ZERO
    if inclusive:
        lines, discount, rounding = from_inclusive(store, lines, discount)
    planned.lines = lines
    planned.subtotal, planned.tax, total = price_lines(lines, discount, 0, store=store)
    planned.total = total + rounding
    planned.rounding, planned.discount, planned.inclusive = rounding, discount, inclusive


def parse_paste(store, text, *, inclusive=False):
    """Pasted rows → ``[Planned]`` in the order their references first appear."""
    products, plans, order = _products(store), {}, []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        cells = [c.strip() for c in (raw.split("\t") if "\t" in raw else raw.split(";"))]
        if not order and cells and cells[0].lower() in HEADER_WORDS:
            continue
        cells += [""] * (11 - len(cells))
        ref, name, issued, due, item, qty, price, discount, phone, address, channel = cells[:11]
        key = ref or f"baris-{number}"   # a row without a reference is an invoice of its own
        planned = plans.get(key)
        if planned is None:
            customer = find_customer(store, name) or (find_customer(store, phone) if phone else None)
            issued_on = parse_date(issued) if issued else timezone.localdate()
            planned = Planned(key=key, customer=customer, buyer_name=(customer.name if customer else name)[:120],
                issued_on=issued_on or timezone.localdate(), due_date=parse_date(due) if due else None,
                reference=ref[:100], new_customer=customer is None and bool(name),
                phone=phone[:30], address=address, channel=channel_for(channel))
            if not name:
                planned.errors.append(f"baris {number}: nama pelanggan wajib")
            if issued and issued_on is None:
                planned.errors.append(f"baris {number}: tanggal “{issued}” tidak terbaca (pakai 31/12/2025)")
            if due and planned.due_date is None:
                planned.errors.append(f"baris {number}: jatuh tempo “{due}” tidak terbaca")
            plans[key] = planned
            order.append(key)
        elif name and name.lower() not in {planned.buyer_name.lower(), (planned.customer.company_name.lower()
                if planned.customer else "")}:
            planned.errors.append(f"baris {number}: pelanggan berbeda dari baris pertama ref {ref}")
        planned.rows.append(number)
        if not item:
            planned.errors.append(f"baris {number}: isi barang (SKU) atau nama jasa")
            continue
        line, error = _line(item, qty, price, discount, products=products, customer=planned.customer)
        if error:
            planned.errors.append(f"baris {number}: {error}")
        else:
            planned.lines.append(line)
    found = [plans[key] for key in order]
    _finish(store, found, inclusive=inclusive)
    return found


def for_customers(store, customers, *, items, issued_on, due_date=None, reference="", inclusive=False):
    """The same lines (``[(text, qty, price, discount)]``) billed to each customer: ``[Planned]``. A product
    without a typed price is priced per customer (their price list)."""
    products = _products(store)
    found = []
    for customer in customers:
        planned = Planned(key=str(customer.pk), customer=customer, buyer_name=customer.name[:120],
            issued_on=issued_on, due_date=due_date, reference=reference[:100])
        for text, qty, price, discount in items:
            if not (text or "").strip():
                continue
            line, error = _line(text, qty, price, discount, products=products, customer=customer)
            if error:
                planned.errors.append(error)
            else:
                planned.lines.append(line)
        found.append(planned)
    _finish(store, found, inclusive=inclusive)
    return found


def _finish(store, plans, *, inclusive):
    for planned in plans:
        if planned.due_date is None and planned.customer is not None:
            planned.due_date = due_date_for(planned.customer, planned.issued_on)
        if planned.due_date and planned.due_date < planned.issued_on:
            planned.errors.append("jatuh tempo sebelum tanggal invoice")
        if not planned.errors:
            _price(store, planned, inclusive=inclusive)


def summary(plans):
    good = [p for p in plans if p.ok]
    return {"count": len(plans), "ready": len(good), "bad": len(plans) - len(good),
        "total": sum((p.total for p in good), ZERO), "new_customers": sorted({p.buyer_name for p in plans
            if p.new_customer})}


@transaction.atomic
def create_all(store, plans, *, actor=None, warehouse=None, credit_check=True):
    """Make every planned invoice, or none: ``[Invoice]``. Raises ``BatchError`` naming the invoice that
    could not be made (stock short, credit limit, a closed period...). A customer not in the CRM yet is added
    by name. Every invoice is on credit (Piutang usaha) with its due date; receive the money afterwards."""
    if not plans:
        raise BatchError("Tidak ada invoice untuk dibuat.")
    if len(plans) > MAX_INVOICES:
        raise BatchError(f"Paling banyak {MAX_INVOICES} invoice sekali buat; bagi daftarnya.")
    bad = [p for p in plans if not p.ok]
    if bad:
        raise BatchError(f"{_label(bad[0])}: {bad[0].errors[0]}.")
    created, added = [], {}
    for planned in plans:
        customer = planned.customer or added.get(planned.buyer_name.lower())
        if customer is None:
            customer = Customer.objects.create(store=store, name=planned.buyer_name, address=planned.address,
                phone=planned.phone if not Customer.objects.filter(store=store, phone=planned.phone).exists() else "",
                status=Customer.STATUS_ACTIVE)
            added[planned.buyer_name.lower()] = customer
        planned.customer = customer
        try:
            created.append(create_sales_invoice(store, lines=planned.lines, buyer_name=planned.buyer_name,
                customer=customer, company_name=customer.company_name, buyer_npwp=customer.npwp or "",
                phone=customer.phone or planned.phone or "-", address=customer.address or planned.address or "-",
                channel=planned.channel, issued_on=planned.issued_on,
                due_date=planned.due_date, on_credit=True, external_order_id=planned.reference,
                discount=planned.discount, rounding=planned.rounding, warehouse=warehouse, actor=actor,
                credit_check=credit_check, prices_include_tax=planned.inclusive))
        except (InvoiceError, LedgerError) as error:
            raise BatchError(f"{_label(planned)}: {error}") from error
    return created


def _label(planned):
    where = f"ref {planned.reference}" if planned.reference else (
        f"baris {planned.rows[0]}" if planned.rows else planned.buyer_name)
    return f"Invoice {where} ({planned.buyer_name})" if planned.reference or planned.rows else planned.buyer_name
