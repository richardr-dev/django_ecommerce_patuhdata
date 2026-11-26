"""Impor penerimaan: many customer payments in one go, every one through ``receipts.receive_payment`` (so each
is an ordinary Penerimaan pembayaran with its journal). Rows are ``tanggal; pelanggan; no. penerimaan;
no. invoice; jumlah; metode; rekening; catatan; diskon``. A discount given at settlement (Accurate's TOTAL DISCOUNT)
is a credit note "Diskon pelunasan" per invoice (sale and PPN come down; the payment stays the money received). A
discount on a row is that invoice's; Accurate's one total per receipt is spread over the receipt's invoices in order,
as far as each still has a balance. Rows with the same customer, receipt number (else date, method
and account) are one receipt that settles several invoices. ``plan`` checks everything and writes nothing;
``create_all`` makes all receipts or none."""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting import bank as bank_service
from apps.accounting.ledger import LedgerError
from apps.accounting.opening_paste import parse_amount, parse_date
from apps.shops.models import Customer

from . import receipts
from .invoice_batch import find_customer
from .models import Invoice, InvoicePayment

MAX_RECEIPTS = 300
METHODS = {"tunai": "cash", "cash": "cash", "transfer": "transfer", "transfer bank": "transfer",
           "qris": "qris", "cod": "cod", "payment link": "transfer", "virtual account": "transfer",
           "non tunai": "transfer", "cek/giro": "other", "giro": "other", "cek": "other", "edc": "other", "kartu kredit": "other", "kartu debit": "other", "e-wallet": "other"}


@dataclass
class PlannedReceipt:
    key: tuple
    customer: Customer | None
    on: date
    method: str = "transfer"
    bank: object = None
    number: str = ""
    note: str = ""
    allocations: list = field(default_factory=list)     # [(invoice, amount)]
    discounts: list = field(default_factory=list)       # [(invoice, discount incl. PPN)]
    pool: Decimal = Decimal(0)                          # a receipt-wide discount still to spread
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    rows: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    @property
    def total(self):
        return sum((amount for _inv, amount in self.allocations), Decimal(0))


def _customer(store, text):
    found = Customer.objects.filter(store=store, customer_code__iexact=text).first() if text else None
    return found or find_customer(store, text)


def _find_invoice(store, text, customer):
    """The invoice a row names: our own number first, else the old system's number that an imported invoice
    keeps as its reference (``Order.external_order_id``), preferring the named customer's open invoice."""
    invoices = Invoice.objects.filter(store=store).select_related("order")
    found = invoices.filter(number__iexact=text).first()
    if found is not None:
        return found
    candidates = list(invoices.filter(order__external_order_id__iexact=text).exclude(status="void"))
    if customer is not None:
        candidates = [i for i in candidates if i.order.customer_id == customer.pk] or candidates
    return next((i for i in candidates if i.status != "paid"), candidates[0] if candidates else None)


def plan(store, text, *, default_bank=None):
    banks = list(bank_service.bank_accounts(store))
    plans, order, spent = {}, [], {}
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        cells = [c.strip() for c in raw.split("\t")] + [""] * 9
        day, who, ref, inv_no, value, method, bank_text, note, disc_text = cells[:9]
        if number == 1 and who.lower() in {"pelanggan", "customer no"}:
            continue
        customer = _customer(store, who)
        on = parse_date(day) if day else timezone.localdate()
        bank, warn = default_bank, ""
        if bank_text:
            bank = next((b for b in banks if bank_text in {b.account.code, b.account_number, b.name}), None)
            if bank is None:
                bank, warn = default_bank, f"rekening “{bank_text}” tidak dikenal; dipakai rekening pilihan Anda"
        method_code = METHODS.get(method.lower(), "transfer") if method else "transfer"
        key = (customer.pk if customer else who.lower(), ref or (str(on), method_code, bank.pk if bank else 0))
        planned = plans.get(key)
        if planned is None:
            planned = PlannedReceipt(key=key, customer=customer, on=on or timezone.localdate(),
                method=method_code, bank=bank, number=ref, note=note)
            if customer is None:
                planned.errors.append(f"baris {number}: pelanggan “{who}” tidak ada di Pelanggan")
            if day and on is None:
                planned.errors.append(f"baris {number}: tanggal “{day}” tidak terbaca (pakai 31/12/2026)")
            plans[key] = planned
            order.append(key)
        if warn and warn not in planned.warnings:
            planned.warnings.append(warn)
        planned.rows.append(number)
        amount = parse_amount(value) if value else None
        invoice = _find_invoice(store, inv_no, customer) if inv_no else None
        if not inv_no or amount is None or amount <= 0:
            planned.errors.append(f"baris {number}: isi nomor invoice dan jumlah lebih dari nol")
        elif invoice is None:
            planned.errors.append(f"baris {number}: invoice {inv_no} tidak ditemukan")
        elif customer is not None and invoice.order.customer_id not in {None, customer.pk}:
            planned.errors.append(f"baris {number}: invoice {inv_no} milik pelanggan lain")
        elif invoice.status in {"void", "paid"} or invoice.amount_due <= 0:
            planned.errors.append(f"baris {number}: invoice {inv_no} sudah lunas atau dibatalkan")
        else:
            already = spent.get(invoice.pk, Decimal(0))
            if amount + already > invoice.amount_due:
                planned.errors.append(f"baris {number}: {inv_no} sisa tagihan hanya "
                    f"Rp{invoice.amount_due - already:,.0f}".replace(",", "."))
            else:
                spent[invoice.pk] = already + amount
                planned.allocations.append((invoice, amount))
        if disc_text:
            discount = parse_amount(disc_text)
            if discount is None or discount < 0:
                planned.errors.append(f"baris {number}: diskon “{disc_text}” tidak terbaca")
            elif discount:
                planned.pool += discount
    for key in order:
        _spread_discount(plans[key], spent)
    return [plans[k] for k in order]


def _spread_discount(planned, spent):
    """Give the receipt's discount to its invoices in order, up to what each still owes after this payment."""
    left = planned.pool
    for invoice, _amount in planned.allocations:
        if left <= 0:
            break
        room = invoice.amount_due - spent.get(invoice.pk, Decimal(0))
        take = min(room, left)
        if take > 0:
            planned.discounts.append((invoice, take))
            spent[invoice.pk] = spent.get(invoice.pk, Decimal(0)) + take
            left -= take
    if left > 0 and planned.allocations:
        planned.errors.append(f"diskon Rp{planned.pool:,.0f} melebihi sisa tagihan setelah pembayaran".replace(",", "."))


def _discount_note(invoice, amount, on, actor, receipt_number):
    """A credit note "Diskon pelunasan" worth ``amount`` with PPN, so the invoice is settled and PPN comes down."""
    from .credit_notes import create_credit_note
    rate = invoice.tax_rate or Decimal(0)
    base = (amount * 100 / (100 + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    while base > 0 and base + (base * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) > amount:
        base -= Decimal("0.01")
    return create_credit_note(invoice, issued_on=on, reason="discount", actor=actor,
        notes=f"Diskon pelunasan{' ' + receipt_number if receipt_number else ''}",
        lines=[{"item": None, "description": "Diskon pelunasan", "quantity": 1, "unit_price": base}])


def summary(plans):
    good = [p for p in plans if p.ok]
    return {"count": len(plans), "ready": len(good), "bad": len(plans) - len(good),
            "total": sum((p.total for p in good), Decimal(0)),
            "discount": sum((d for p in good for _i, d in p.discounts), Decimal(0))}


@transaction.atomic
def create_all(store, plans, *, actor=None):
    """Make every receipt, or none: ``[CustomerReceipt]``."""
    if not plans:
        raise receipts.ReceiptError("Tidak ada penerimaan untuk dibuat.")
    if len(plans) > MAX_RECEIPTS:
        raise receipts.ReceiptError(f"Paling banyak {MAX_RECEIPTS} penerimaan sekali impor; bagi daftarnya.")
    bad = [p for p in plans if not p.ok]
    if bad:
        raise receipts.ReceiptError(f"{bad[0].customer.name if bad[0].customer else 'Penerimaan'}: {bad[0].errors[0]}.")
    made = []
    for planned in plans:
        try:
            made.append(receipts.receive_payment(store, customer=planned.customer,
                buyer_name=planned.customer.name, date=planned.on, amount=planned.total,
                method=planned.method if planned.method in dict(InvoicePayment.METHOD_CHOICES) else "transfer",
                allocations=planned.allocations, reference=planned.number, bank_account=planned.bank,
                note=planned.note or "Impor penerimaan", keep_rest=False, actor=actor))
            for invoice, discount in planned.discounts:
                _discount_note(invoice, discount, planned.on, actor, planned.number)
        except (receipts.ReceiptError, LedgerError, ValueError) as error:
            raise receipts.ReceiptError(f"{planned.customer.name}: {error}") from error
    return made
