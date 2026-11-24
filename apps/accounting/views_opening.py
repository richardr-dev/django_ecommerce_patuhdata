"""Saldo awal › Piutang per invoice: the old system's unpaid invoices, one by one or pasted from Excel."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import Customer
from apps.shops.permissions import can, roles
from apps.tax import opening_invoices as service
from apps.tax.models import Invoice

from . import idempotency

SCOPE = "opening_invoice"


def _date(text):
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        raise service.OpeningInvoiceError("Tanggal tidak terbaca.") from None


def _amount(text):
    try:
        return Decimal(str(text or "0").replace(".", "").replace(",", "") or "0")
    except InvalidOperation:
        raise service.OpeningInvoiceError("Sisa tagihan tidak terbaca.") from None


def _one(store, post, actor):
    name = post.get("customer", "").strip()
    if not name:
        raise service.OpeningInvoiceError("Isi nama pelanggan.")
    customer = service.find_customer(store, name) or Customer.objects.create(store=store, name=name[:120])
    return [service.create_opening_invoice(store, customer=customer, number=post.get("number", ""),
        issued_on=_date(post.get("issued_on")), due_date=_date(post.get("due_date")),
        amount=_amount(post.get("amount")), actor=actor)]


@login_required(login_url="/login/")
def merchant_opening_invoices(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("books.view"))
    post = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        _require_roles(request, store, roles("books.edit"))
        idem = post.get("idem", "")
        if idempotency.claim(store, SCOPE, idem) is not None:
            messages.info(request, "Sudah dicatat.")
            return redirect("merchant-opening-invoices")
        try:
            if post.get("action") == "paste":
                created = service.import_rows(store, service.parse(post.get("rows", "")), actor=request.user)
            else:
                created = _one(store, post, request.user)
        except service.InvoiceError as error:
            idempotency.release(store, SCOPE, idem)
            messages.error(request, str(error))
        else:
            idempotency.finish(store, SCOPE, idem, created[-1].pk)
            messages.success(request, f"{len(created)} invoice saldo awal dicatat." if len(created) > 1
                else f"Invoice {created[0].number} dicatat sebagai saldo awal.")
            return redirect("merchant-opening-invoices")
    invoices = list(service.opening_invoices(store))
    count, original, owed = service.totals(store)
    return render(request, "merchant/opening_invoices.html", _merchant_context(request, store,
        invoices=invoices, count=count, original=original, owed=owed,
        lump=service.receivable_in_opening_entry(store), start=service.books_start(store),
        customers=Customer.objects.filter(store=store).order_by("name").values_list("name", flat=True)[:2000],
        can_edit=can(request.user, store, "books.edit"), post=post, idem_key=idempotency.new_key(),
        void=Invoice.STATUS_VOID))


# ── Saldo awal › Utang per tagihan ──────────────────────────────────────────

BILL_SCOPE = "opening_bill"


def _one_bill(store, post, actor):
    from . import opening_bills
    from .models import Supplier
    name = post.get("supplier", "").strip()
    if not name:
        raise opening_bills.OpeningBillError("Isi nama supplier.")
    supplier = opening_bills.find_supplier(store, name) or Supplier.objects.create(store=store, name=name[:160])
    return [opening_bills.create_opening_bill(store, supplier=supplier, number=post.get("number", ""),
        bill_date=_date(post.get("bill_date")), due_date=_date(post.get("due_date")),
        amount=_amount(post.get("amount")), actor=actor)]


@login_required(login_url="/login/")
def merchant_opening_bills(request):
    from . import opening_bills
    from .models import Bill, Supplier
    from .payables import PayableError
    store = _staff_store(request)
    _require_roles(request, store, roles("books.view"))
    post = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        _require_roles(request, store, roles("books.edit"))
        idem = post.get("idem", "")
        if idempotency.claim(store, BILL_SCOPE, idem) is not None:
            messages.info(request, "Sudah dicatat.")
            return redirect("merchant-opening-bills")
        try:
            if post.get("action") == "paste":
                created = opening_bills.import_rows(store, opening_bills.parse(post.get("rows", "")), actor=request.user)
            else:
                created = _one_bill(store, post, request.user)
        except (PayableError, service.InvoiceError) as error:
            idempotency.release(store, BILL_SCOPE, idem)
            messages.error(request, str(error))
        else:
            idempotency.finish(store, BILL_SCOPE, idem, str(created[-1].pk))
            messages.success(request, f"{len(created)} tagihan saldo awal dicatat." if len(created) > 1
                else f"Tagihan {created[0].number} dicatat sebagai saldo awal.")
            return redirect("merchant-opening-bills")
    count, original, owed = opening_bills.totals(store)
    return render(request, "merchant/opening_bills.html", _merchant_context(request, store,
        bills=list(opening_bills.opening_bills(store)), count=count, original=original, owed=owed,
        lump=opening_bills.payable_in_opening_entry(store), start=opening_bills.books_start(store),
        suppliers=Supplier.objects.filter(store=store).order_by("name").values_list("name", flat=True)[:2000],
        can_edit=can(request.user, store, "books.edit"), post=post, idem_key=idempotency.new_key(),
        void=Bill.STATUS_VOID))
