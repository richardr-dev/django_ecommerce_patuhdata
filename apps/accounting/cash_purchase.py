"""Beli tunai: goods bought and paid on the spot (a shop owner at the market or the wholesaler).

One step for the person, three ordinary documents underneath, in one transaction: a bill for the
goods, the goods received onto the shelf (moving average cost, open stok minus covered first), and
the bill paid in full from the chosen cash or bank account. Nothing here writes a journal line: the
bill posts Persediaan / Utang usaha and the payment Utang usaha / Kas, so Utang usaha nets to zero
and every existing report, control and the bill page itself see a normal, paid bill.

Segregation of duties: the buyer both records and pays, which a cash purchase is by nature. The
store's bill-payment approval threshold still applies (the view refuses above it and sends the
person to the ordinary bill, which is paid after approval), and the purchase is in the audit log."""
from decimal import Decimal

from django.db import transaction

from .models import Bill
from .payables import PayableError, _find_goods, create_bill, pay_bill, receive_bill


class CashPurchaseError(ValueError):
    pass


def method_for(bank_account):
    """How the money left: a cash box is "Tunai", anything else a transfer."""
    return "cash" if bank_account is not None and bank_account.kind == "cash" else (
        "transfer" if bank_account is not None else "cash")


@transaction.atomic
def buy_for_cash(store, *, supplier, lines, purchase_date, bank_account=None, warehouse=None,
        tax=Decimal(0), number="", notes="", actor=None):
    """``lines``: dicts with ``sku``, ``description``, ``qty`` (whole units) and ``unit_price``
    (before PPN). Every line must be a product or variant in the catalog: an expense bought for
    cash is "Biaya operasional", not this. ``bank_account`` None pays from Kas utama. Returns the
    paid bill."""
    if supplier is None:
        raise CashPurchaseError("Pilih atau tulis nama supplier.")
    if not lines:
        raise CashPurchaseError("Tambahkan minimal satu barang.")
    for line in lines:
        product, _variant = _find_goods(store, line.get("sku", ""))
        if product is None:
            name = line.get("description") or line.get("sku") or "tanpa nama"
            raise CashPurchaseError(f"Barang \"{name}\" tidak ada di katalog. Pilih produk dari daftar; "
                "biaya selain barang dagang dicatat di Biaya operasional.")
        if line["qty"] <= 0 or line["qty"] != int(line["qty"]):
            raise CashPurchaseError(f"Jumlah {product.name} harus bilangan bulat lebih dari nol.")
        if line["unit_price"] < 0:
            raise CashPurchaseError(f"Harga {product.name} tidak boleh negatif.")
        if not line.get("description"):
            line["description"] = product.name
    try:
        bill = create_bill(store, supplier=supplier, lines=lines, bill_date=purchase_date,
            due_date=purchase_date, number=number, tax=tax, category="1400",
            notes=(notes or "Beli tunai")[:300], actor=actor)
        receive_bill(bill, warehouse=warehouse, actor=actor)
        pay_bill(bill, amount=bill.total, paid_on=purchase_date, method=method_for(bank_account),
            reference=number[:120], actor=actor, bank_account=bank_account)
    except PayableError as error:
        raise CashPurchaseError(str(error)) from error
    return Bill.objects.get(pk=bill.pk)
