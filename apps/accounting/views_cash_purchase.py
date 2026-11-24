"""Beli tunai page: one form that buys goods, puts them on the shelf and pays for them."""
import datetime
from decimal import Decimal, InvalidOperation
from itertools import zip_longest

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.catalog.models import Product, ProductVariant, Warehouse
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.audit import log as audit_log
from apps.shops.permissions import roles

from . import approvals, attachments, idempotency
from .bank import bank_accounts
from .cash_purchase import CashPurchaseError, buy_for_cash
from .models import Supplier

SCOPE = "cash_purchase"


def _catalog(store):
    """What can be bought: every active product and variant with its SKU, last cost and barcode,
    for the picker (a scanned barcode resolves to its SKU in the browser)."""
    items = [{"sku": p.sku, "name": p.name, "cost": str(p.cost_price or 0), "barcode": ""}
        for p in Product.objects.filter(store=store, is_active=True, is_digital=False).exclude(sku="")
            .only("sku", "name", "cost_price").order_by("name")]
    items += [{"sku": v.sku, "name": f"{v.product.name} · {v.name}",
        "cost": str(v.cost_price or v.product.cost_price or 0), "barcode": v.barcode}
        for v in ProductVariant.objects.filter(product__store=store, product__is_active=True, is_active=True)
            .exclude(sku="").select_related("product").order_by("product__name", "name")]
    return items


def _rows(post):
    rows = [{"sku": sku.strip(), "desc": desc.strip(), "qty": qty, "price": price}
        for sku, desc, qty, price in zip_longest(post.getlist("sku[]"), post.getlist("description[]"),
            post.getlist("qty[]"), post.getlist("unit_price[]"), fillvalue="")]
    return [row for row in rows if row["sku"] or row["desc"] or row["price"]]


def _amount(text):
    return Decimal(str(text or "0").replace(".", "").replace(",", "") or "0")


def _lines(rows):
    lines = []
    for row in rows:
        try:
            qty, price = Decimal(row["qty"] or "0"), _amount(row["price"])
        except InvalidOperation:
            raise CashPurchaseError(f"Jumlah atau harga {row['desc'] or row['sku']} tidak terbaca.") from None
        lines.append({"sku": row["sku"], "description": row["desc"], "qty": qty, "unit_price": price})
    return lines


def _supplier(store, post):
    chosen = post.get("supplier_id", "")
    if chosen.isdigit():
        found = Supplier.objects.filter(store=store, pk=int(chosen)).first()
        if found:
            return found
    name = post.get("supplier_name", "").strip()[:160]
    if name:
        return Supplier.objects.get_or_create(store=store, name=name)[0]
    return None


@login_required(login_url="/login/")
def merchant_cash_purchase(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("purchase.cash"))
    banks = bank_accounts(store)
    warehouses = list(Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id"))
    today = timezone.localdate()
    if request.method == "POST":
        post = request.POST
        idem = post.get("idem", "")
        earlier = idempotency.claim(store, SCOPE, idem)
        if earlier is not None:
            messages.info(request, "Pembelian ini sudah dicatat.")
            return redirect("merchant-bill-detail", bill_id=earlier) if earlier else redirect("merchant-bills")
        upload = request.FILES.get("nota")
        try:
            if upload:
                attachments.validate(upload)  # before anything is booked
            when = datetime.date.fromisoformat(post.get("purchase_date") or today.isoformat())
            if when > today:
                raise CashPurchaseError("Tanggal pembelian tidak boleh di masa depan.")
            lines = _lines(_rows(post))
            tax = _amount(post.get("tax"))
            total = sum((line["qty"] * line["unit_price"] for line in lines), Decimal(0)) + tax
            if approvals.required(store, "bill_payment", total, request.user):
                raise CashPurchaseError("Nilainya di atas batas persetujuan pembayaran. Catat sebagai "
                    "Tagihan supplier; pembayarannya diajukan untuk disetujui orang lain.")
            bank = next((b for b in banks if str(b.pk) == post.get("bank_account")), None)
            warehouse = next((w for w in warehouses if str(w.pk) == post.get("warehouse")), None)
            bill = buy_for_cash(store, supplier=_supplier(store, post), lines=lines, purchase_date=when,
                bank_account=bank, warehouse=warehouse, tax=tax, number=post.get("number", "")[:40],
                notes=post.get("notes", "")[:300], actor=request.user)
        except (CashPurchaseError, attachments.AttachmentError, ValueError) as error:
            idempotency.release(store, SCOPE, idem)
            messages.error(request, str(error))
        else:
            if upload:
                attachments.add(store, "bill", bill.pk, upload, actor=request.user, note="Nota pembelian")
            idempotency.finish(store, SCOPE, idem, bill.pk)
            audit_log(store, request.user, "bill.cash_purchase", bill, total=bill.total,
                supplier=bill.supplier.name)
            messages.success(request, "Pembelian tunai dicatat: barang masuk stok dan kas berkurang.")
            return redirect("merchant-bill-detail", bill_id=bill.pk)
    post = request.POST if request.method == "POST" else {}
    return render(request, "merchant/cash_purchase.html", _merchant_context(request, store,
        suppliers=Supplier.objects.filter(store=store, is_active=True).order_by("name"),
        banks=banks, warehouses=warehouses if len(warehouses) > 1 else [], catalog=_catalog(store),
        rows=_rows(request.POST) if request.method == "POST" else [], post=post,
        today=today.isoformat(), idem_key=idempotency.new_key()))
