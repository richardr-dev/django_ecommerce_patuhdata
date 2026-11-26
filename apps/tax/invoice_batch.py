"""Buat banyak invoice: many sales invoices in one go, every one through ``create_sales_invoice``, so they
price, tax, take stock and post exactly like an invoice made on the form.

Two ways in, one plan:

- **Paste from Excel**: one row per line, ``no. ref; pelanggan; tanggal; jatuh tempo; barang atau jasa; jumlah;
  harga; diskon; telepon; alamat; saluran; ongkir ditagih; ongkir dibayar; status`` (tab or ``;``). Rows that share a reference are one invoice; the reference is kept as the
  invoice's Referensi. "Barang atau jasa" is a product SKU (stock and HPP) or else the text of a service line.
  An empty price takes the customer's price list, else the product's price.
  "Saluran" naming a mitra (its name inside the text, e.g. "Shopee ABShop") records the sale under that mitra.
  "Ongkir ditagih" is charged to the buyer (4200); "ongkir dibayar" is the courier cost the company paid: an
  Expense (6700 Transportasi & ongkos kirim) from the account chosen on the page, one per invoice.
  "Status" ``retur`` marks a line that came back: the invoice is made as usual, then one credit note
  ("Barang diretur", restocked) for those lines on the invoice date, so the sale and its return are both on the
  books and the stock is back.
- **The same lines to many customers** (a monthly fee, a rent): the lines once, the customers ticked.

``plan`` reads the input and prices every invoice without writing anything, naming what is wrong per invoice,
so the page can show a preview. ``create_all`` makes every planned invoice or none (one transaction: an
invoice that fails rolls back the ones before it, and the gapless numbers with them)."""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import LedgerError
from apps.accounting.opening_paste import parse_amount, parse_date
from apps.catalog.models import Product, ProductVariant
from apps.shops.models import Customer

from .credit_notes import CreditNoteError, create_credit_note
from .pricing import customer_price, due_date_for
from .sales import clean_lines, create_sales_invoice, from_inclusive, price_lines
from .services import InvoiceError, vat_for

ZERO = Decimal(0)
MAX_INVOICES = 300
HEADER_WORDS = {"no", "no.", "ref", "referensi", "no ref", "no. ref", "nomor", "kelompok"}
COLUMNS = ["No. ref", "Pelanggan", "Tanggal", "Jatuh tempo", "Barang (SKU) atau jasa", "Jumlah", "Harga",
    "Diskon", "Telepon", "Alamat", "Saluran", "Ongkir ditagih", "Ongkir dibayar", "Status"]
RETURN_WORDS = ("retur", "return", "dikembalikan", "refund")
SHIPPING_COST_CATEGORY = "6700"   # Transportasi & ongkos kirim
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
    shipping: Decimal = ZERO                       # ongkir charged to the buyer (4200, no PPN)
    shipping_cost: Decimal = ZERO                  # ongkir the company paid the courier (an Expense)
    partner: object = None                         # the mitra the sale went through
    channel_text: str = ""
    returned: list = field(default_factory=list)   # indexes of lines that came back (credit note)
    returned_total: Decimal = ZERO                 # what the credit note will take off, PPN included
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


def _squash(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _partners(store):
    from apps.settlements.models import ConsignmentPartner
    found = [(_squash(p.name), p) for p in ConsignmentPartner.objects.filter(store=store, is_active=True)]
    return sorted([item for item in found if len(item[0]) >= 3], key=lambda item: -len(item[0]))


def partner_for(partners, text):
    """The mitra whose name appears in a sales-channel text ("Shopee ABShop" → ABShop), longest name first."""
    squashed = _squash(text)
    return next((partner for name, partner in partners if name in squashed), None)


def _fee(text, label, planned, number):
    value = parse_amount(text)
    if value is None or value < 0:
        planned.errors.append(f"baris {number}: {label} “{text}” tidak terbaca")
        return ZERO
    return value


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
    planned.subtotal, planned.tax, total = price_lines(lines, discount, planned.shipping, store=store)
    planned.total = total + rounding
    planned.rounding, planned.discount, planned.inclusive = rounding, discount, inclusive
    if planned.returned:
        back = sum((_credit_price(line) * line["qty"] for i, line in enumerate(lines) if i in planned.returned), ZERO)
        planned.returned_total = back + (vat_for(store, back)[1] if planned.tax else ZERO)


def parse_paste(store, text, *, inclusive=False):
    """Pasted rows → ``[Planned]`` in the order their references first appear."""
    products, plans, order, partners = _products(store), {}, [], _partners(store)
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        cells = [c.strip() for c in (raw.split("\t") if "\t" in raw else raw.split(";"))]
        if not order and cells and cells[0].lower() in HEADER_WORDS:
            continue
        cells += [""] * (14 - len(cells))
        ref, name, issued, due, item, qty, price, discount, phone, address, channel, ongkir, paid, status = cells[:14]
        key = ref or f"baris-{number}"   # a row without a reference is an invoice of its own
        planned = plans.get(key)
        if planned is None:
            customer = find_customer(store, name) or (find_customer(store, phone) if phone else None)
            issued_on = parse_date(issued) if issued else timezone.localdate()
            planned = Planned(key=key, customer=customer, buyer_name=(customer.name if customer else name)[:120],
                issued_on=issued_on or timezone.localdate(), due_date=parse_date(due) if due else None,
                reference=ref[:100], new_customer=customer is None and bool(name),
                phone=phone[:30], address=address, channel=channel_for(channel),
                partner=partner_for(partners, channel), channel_text=channel[:100])
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
        if ongkir:
            planned.shipping += _fee(ongkir, "ongkir", planned, number)
        if paid:
            planned.shipping_cost += _fee(paid, "ongkir dibayar", planned, number)
        if not item and (ongkir or paid):
            continue        # a row carrying only the ongkir of the invoice above
        if not item:
            planned.errors.append(f"baris {number}: isi barang (SKU) atau nama jasa")
            continue
        line, error = _line(item, qty, price, discount, products=products, customer=planned.customer)
        if error:
            planned.errors.append(f"baris {number}: {error}")
        else:
            if any(word in status.lower() for word in RETURN_WORDS):
                planned.returned.append(len(planned.lines))
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
        if not planned.lines and not planned.errors:
            planned.errors.append("tidak ada baris barang atau jasa")
        if planned.due_date is None and planned.customer is not None:
            planned.due_date = due_date_for(planned.customer, planned.issued_on)
        if planned.due_date and planned.due_date < planned.issued_on:
            planned.errors.append("jatuh tempo sebelum tanggal invoice")
        if not planned.errors:
            _price(store, planned, inclusive=inclusive)


def summary(plans):
    good = [p for p in plans if p.ok]
    return {"count": len(plans), "ready": len(good), "bad": len(plans) - len(good),
        "shipping_cost": sum((p.shipping_cost for p in good), ZERO),
        "returns": sum(1 for p in good if p.returned), "returned_total": sum((p.returned_total for p in good), ZERO),
        "total": sum((p.total for p in good), ZERO), "new_customers": sorted({p.buyer_name for p in plans
            if p.new_customer})}


@transaction.atomic
def create_all(store, plans, *, actor=None, warehouse=None, credit_check=True, cost_account=None):
    """Make every planned invoice, or none: ``[Invoice]``. Raises ``BatchError`` naming the invoice that
    could not be made (stock short, credit limit, a closed period...). A customer not in the CRM yet is added
    by name. Every invoice is on credit (Piutang usaha) with its due date; receive the money afterwards.
    Ongkir the company paid becomes one Expense per invoice from ``cost_account`` (a ``BankAccount``; the main
    cash account when None), through the expense approval like any other."""
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
        notes = f"Saluran: {planned.channel_text}" if planned.partner and planned.channel_text else ""
        try:
            created.append(create_sales_invoice(store, lines=planned.lines, buyer_name=planned.buyer_name,
                customer=customer, company_name=customer.company_name, buyer_npwp=customer.npwp or "",
                phone=customer.phone or planned.phone or "-", address=customer.address or planned.address or "-",
                channel=planned.channel, issued_on=planned.issued_on,
                due_date=planned.due_date, on_credit=True, external_order_id=planned.reference,
                discount=planned.discount, shipping=planned.shipping, rounding=planned.rounding, warehouse=warehouse, actor=actor,
                credit_check=credit_check, prices_include_tax=planned.inclusive, partner=planned.partner,
                notes=notes))
            if planned.shipping_cost:
                _shipping_expense(store, created[-1], planned.shipping_cost, cost_account, actor)
            if planned.returned:
                _credit_returns(created[-1], planned.returned, actor)
        except (InvoiceError, LedgerError, CreditNoteError) as error:
            raise BatchError(f"{_label(planned)}: {error}") from error
    return created


def _credit_price(line):
    """The unit price a returned line is credited at: what the buyer was charged per unit, after its discount."""
    qty = Decimal(line["qty"])
    net = qty * Decimal(line["unit_price"]) - Decimal(line.get("discount") or 0)
    return (net / qty).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _credit_returns(invoice, indexes, actor):
    """One credit note for the lines that came back, restocked, dated with the invoice."""
    items = list(invoice.items.order_by("id"))
    lines = [{"item": items[i], "quantity": items[i].quantity, "restock": bool(items[i].product_id),
        "unit_price": _credit_price({"qty": items[i].quantity, "unit_price": items[i].unit_price,
            "discount": items[i].discount})} for i in indexes]
    return create_credit_note(invoice, lines=lines, issued_on=invoice.issued_on, reason="return",
        notes="Retur dari impor penjualan", actor=actor)


def _shipping_expense(store, invoice, amount, cost_account, actor):
    from apps.accounting import approvals
    from apps.accounting.ledger import post_expense
    from apps.accounting.models import Expense
    from apps.shops.models import AuditLog

    expense = Expense.objects.create(store=store, date=invoice.issued_on, category=SHIPPING_COST_CATEGORY,
        description=f"Ongkir {invoice.number} · {invoice.buyer_name}"[:200], amount=amount,
        bank_account=cost_account, created_by=actor)
    if actor is not None and approvals.required(store, "expense", amount, actor):
        expense.approval_pending = True
        expense.save(update_fields=["approval_pending"])
        approvals.submit(store, "expense", amount, f"Biaya: ongkos kirim · {expense.description}",
            {"expense_id": expense.pk, "pph_kind": "", "pph_rate": ""}, actor, subject=expense)
    else:
        post_expense(expense)
    AuditLog.objects.create(store=store, actor=actor, action="expense.created", object_type="Expense",
        object_id=str(expense.pk), details={"amount": str(amount), "category": SHIPPING_COST_CATEGORY,
            "label": expense.description})
    return expense


def _label(planned):
    where = f"ref {planned.reference}" if planned.reference else (
        f"baris {planned.rows[0]}" if planned.rows else planned.buyer_name)
    return f"Invoice {where} ({planned.buyer_name})" if planned.reference or planned.rows else planned.buyer_name
