"""Back-office pages for credit notes, customer receipts and credit, quotes, and recurring
invoices."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.accounting import bank as bank_service
from apps.accounting.ledger import LedgerError, entries_for
from apps.catalog.models import Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.audit import trail as audit_trail
from apps.shops.models import AuditLog, Customer
from apps.shops.permissions import roles

from . import credit_notes, quotes, receipts, recurring
from .models import (
    CreditNote,
    CustomerReceipt,
    Invoice,
    InvoicePayment,
    Quote,
    RecurringInvoice,
)
from .services import InvoiceError

LOGIN = "/login/"
VIEW_ROLES = roles("sales.view")
EDIT_ROLES = roles("sales.edit")


def _store(request, roles=VIEW_ROLES):
    store = _staff_store(request)
    _require_roles(request, store, roles)
    return store


def _can_edit(request, store):
    return store.owner_id == request.user.id or store.staff_members.filter(
        user=request.user, role__in=EDIT_ROLES).exists()


def _date(value, default=None):
    return parse_date(value or "") or default


def _amount(value):
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return Decimal(0)
    parsed = bank_service.parse_amount(text)
    if parsed is None:
        raise InvalidOperation(value)
    return parsed


def _audit(request, store, action, obj, **details):
    AuditLog.objects.create(store=store, actor=request.user, action=action,
        object_type=type(obj).__name__, object_id=str(obj.pk), details=details)


def _customer(store, value):
    if not value:
        return None
    try:
        return Customer.objects.filter(store=store, pk=value).first()
    except (ValueError, ValidationError):  # malformed uuid from the query string
        return None


def _product_lines(request, store):
    """Lines of a quote/recurring form: ``product[]``, ``qty[]``, ``unit_price[]``."""
    lines = []
    for product_id, qty, price in zip(request.POST.getlist("product[]"),
            request.POST.getlist("qty[]"), request.POST.getlist("unit_price[]"), strict=False):
        if not product_id:
            continue
        product = Product.objects.filter(store=store, pk=product_id).first()
        quantity = int(Decimal(qty or 0))
        if not product or quantity <= 0:
            continue
        lines.append({"product": product, "qty": quantity, "unit_price": _amount(price)})
    return lines


def _posted(request):
    """The submitted values with "" for anything missing, so a template can use one as the
    argument of a filter (``|default:post.name``) without failing on a first visit."""
    return defaultdict(str, request.POST.items())


def _banks(store):
    return bank_service.bank_accounts(store)


def _bank(store, value):
    if not value:
        return None
    return next((b for b in _banks(store) if str(b.pk) == str(value)), None)


# ---------------------------------------------------------------------------
# Nota kredit
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_credit_notes(request):
    store = _store(request)
    notes = CreditNote.objects.filter(store=store).select_related("invoice")
    query = request.GET.get("q", "").strip()
    if query:
        notes = notes.filter(Q(number__icontains=query) | Q(buyer_name__icontains=query)
            | Q(invoice__number__icontains=query))
    live = notes.exclude(status=CreditNote.STATUS_VOID)
    totals = live.aggregate(total=Sum("total", default=0), tax=Sum("tax", default=0))
    page = Paginator(notes, 50).get_page(request.GET.get("page"))
    return render(request, "merchant/credit_notes.html", _merchant_context(request, store,
        page=page, query=query, totals=totals, count=live.count(),
        can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_credit_note_new(request):
    store = _store(request, EDIT_ROLES)
    invoice = get_object_or_404(Invoice.objects.select_related("order__customer"),
        pk=request.GET.get("invoice") or request.POST.get("invoice"), store=store)
    rows = credit_notes.creditable(invoice)
    banks = _banks(store)
    if request.method == "POST":
        try:
            lines = []
            for row in rows:
                key = str(row["item"].pk)
                quantity = Decimal(request.POST.get(f"qty_{key}") or 0)
                if quantity <= 0:
                    continue
                lines.append({"item": row["item"], "quantity": quantity,
                    "unit_price": _amount(request.POST.get(f"price_{key}")
                        or row["item"].unit_price),
                    "restock": request.POST.get(f"restock_{key}") == "1"})
            extra_text, extra_amount = request.POST.get("extra_description", "").strip(), \
                _amount(request.POST.get("extra_amount"))
            if extra_amount > 0:
                lines.append({"item": None, "description": extra_text or "Koreksi harga",
                    "quantity": 1, "unit_price": extra_amount, "restock": False})
            note = credit_notes.create_credit_note(invoice, lines=lines,
                issued_on=_date(request.POST.get("issued_on"), timezone.localdate()),
                reason=request.POST.get("reason", "return"), notes=request.POST.get("notes", ""),
                excess_action=request.POST.get("excess_action", "credit"),
                bank_account=_bank(store, request.POST.get("bank_account")), actor=request.user)
        except (credit_notes.CreditNoteError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Jumlah tidak valid.")
        else:
            _audit(request, store, "credit_note.created", note, total=str(note.total),
                invoice=invoice.number)
            messages.success(request, f"Nota kredit {note.number} diterbitkan.")
            return redirect("merchant-credit-note-detail", note.pk)
    return render(request, "merchant/credit_note_new.html", _merchant_context(request, store,
        invoice=invoice, rows=rows, banks=banks, reasons=CreditNote.REASONS,
        today=timezone.localdate().isoformat(), post=request.POST,
        marketplace_channels=CreditNote.MARKETPLACE_CHANNELS,
        due=invoice.amount_due, taxed=bool(invoice.tax_rate)))


@login_required(login_url=LOGIN)
def merchant_credit_note_detail(request, note_id):
    store = _store(request)
    note = get_object_or_404(CreditNote.objects.select_related("invoice", "bank_account",
        "created_by"), pk=note_id, store=store)
    if request.method == "POST" and request.POST.get("action") == "void":
        _require_roles(request, store, EDIT_ROLES)
        try:
            credit_notes.void_credit_note(note, actor=request.user)
        except credit_notes.CreditNoteError as error:
            messages.error(request, str(error))
        else:
            _audit(request, store, "credit_note.voided", note)
            messages.success(request, "Nota kredit dibatalkan dan jurnalnya dibalik.")
        return redirect("merchant-credit-note-detail", note.pk)
    journal = entries_for(store, [("credit_note", note.pk)])
    return render(request, "merchant/credit_note_detail.html", _merchant_context(request, store,
        note=note, items=note.items.all(), journal=journal, can_edit=_can_edit(request, store),
        audit=audit_trail(store, note)))


# ---------------------------------------------------------------------------
# Terima pembayaran & kredit pelanggan
# ---------------------------------------------------------------------------

def _resolve_party(store, request):
    """(customer, buyer_name) chosen by ``?customer=<uuid>`` or ``?buyer=<name>``."""
    customer = _customer(store, request.GET.get("customer") or request.POST.get("customer"))
    name = customer.name if customer else (request.GET.get("buyer")
        or request.POST.get("buyer") or "").strip()
    return customer, name


@login_required(login_url=LOGIN)
def merchant_receive_payment(request):
    """One payment, several invoices."""
    store = _store(request, EDIT_ROLES)
    customer, buyer = _resolve_party(store, request)
    invoices = receipts.open_invoices_for(store, customer, buyer) if (customer or buyer) else []
    if request.method == "POST" and (customer or buyer):
        try:
            by_id = {str(inv.pk): inv for inv in invoices}
            allocations = [(by_id[key[6:]], _amount(value)) for key, value in request.POST.items()
                if key.startswith("alloc_") and key[6:] in by_id and value.strip()]
            receipt = receipts.receive_payment(store, customer=customer, buyer_name=buyer,
                date=_date(request.POST.get("date"), timezone.localdate()),
                amount=_amount(request.POST.get("amount")),
                method=request.POST.get("method", "transfer"),
                reference=request.POST.get("reference", ""),
                bank_account=_bank(store, request.POST.get("bank_account")),
                note=request.POST.get("note", ""), allocations=allocations,
                keep_rest=request.POST.get("keep_rest") == "1", actor=request.user)
        except (receipts.ReceiptError, LedgerError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Jumlah tidak valid.")
        else:
            _audit(request, store, "receipt.created", receipt, amount=str(receipt.amount))
            messages.success(request, f"Penerimaan {receipt.number} dicatat.")
            return redirect("merchant-receipt-detail", receipt.pk)
    customers = Customer.objects.filter(store=store).order_by("name")[:500]
    return render(request, "merchant/receive_payment.html", _merchant_context(request, store,
        customer=customer, buyer=buyer, invoices=invoices, customers=customers, banks=_banks(store),
        methods=[m for m in InvoicePayment.METHOD_CHOICES if m[0] not in {"credit", "cod"}],
        credit=receipts.credit_balance(store, customer, buyer) if (customer or buyer) else 0,
        owed=sum((i.amount_due for i in invoices), Decimal(0)),
        today=timezone.localdate().isoformat(), post=request.POST))


@login_required(login_url=LOGIN)
def merchant_receipts(request):
    store = _store(request)
    rows = CustomerReceipt.objects.filter(store=store).select_related("bank_account")
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(request, "merchant/receipts.html", _merchant_context(request, store,
        page=page, can_edit=_can_edit(request, store),
        total=rows.filter(voided=False).aggregate(t=Sum("amount", default=0))["t"]))


@login_required(login_url=LOGIN)
def merchant_receipt_detail(request, receipt_id):
    store = _store(request)
    receipt = get_object_or_404(CustomerReceipt.objects.select_related("bank_account",
        "created_by", "customer"), pk=receipt_id, store=store)
    if request.method == "POST" and request.POST.get("action") == "void":
        _require_roles(request, store, EDIT_ROLES)
        try:
            receipts.void_receipt(receipt, actor=request.user)
        except (receipts.ReceiptError, LedgerError) as error:
            messages.error(request, str(error))
        else:
            _audit(request, store, "receipt.voided", receipt)
            messages.success(request, "Penerimaan dibatalkan dan jurnalnya dibalik.")
        return redirect("merchant-receipt-detail", receipt.pk)
    allocations = list(receipt.allocations.select_related("invoice"))
    journal = entries_for(store, [("invoice_payment", p.pk) for p in allocations]
        + [("receipt_deposit", receipt.pk)])
    return render(request, "merchant/receipt_detail.html", _merchant_context(request, store,
        receipt=receipt, allocations=allocations, journal=journal,
        can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_customer_credit(request):
    store = _store(request)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        customer, buyer = _resolve_party(store, request)
        try:
            action = request.POST.get("action")
            if action == "refund":
                receipts.refund_credit(store, customer, buyer, _amount(request.POST.get("amount")),
                    _date(request.POST.get("date"), timezone.localdate()),
                    bank_account=_bank(store, request.POST.get("bank_account")),
                    actor=request.user)
                messages.success(request, "Kredit dikembalikan.")
            elif action == "apply":
                invoice = get_object_or_404(Invoice, pk=request.POST.get("invoice"), store=store)
                receipts.apply_credit(invoice, _amount(request.POST.get("amount")),
                    _date(request.POST.get("date"), timezone.localdate()), actor=request.user)
                messages.success(request, f"Kredit dipakai untuk invoice {invoice.number}.")
            else:
                raise Http404
        except (receipts.ReceiptError, LedgerError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Jumlah tidak valid.")
        return redirect("merchant-customer-credit")
    rows = receipts.credit_balances(store)
    for row in rows:
        row["invoices"] = receipts.open_invoices_for(store, row["customer"], row["name"])
    return render(request, "merchant/customer_credit.html", _merchant_context(request, store,
        rows=rows, banks=_banks(store), total=sum((r["balance"] for r in rows), Decimal(0)),
        today=timezone.localdate().isoformat(), can_edit=_can_edit(request, store)))


# ---------------------------------------------------------------------------
# Penawaran
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_quotes(request):
    store = _store(request)
    status = request.GET.get("status", "")
    rows = Quote.objects.filter(store=store)
    if status:
        rows = rows.filter(status=status)
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    today = timezone.localdate()
    for quote in page:
        quote.expired = quotes.is_expired(quote, today)
    open_total = Quote.objects.filter(store=store, status__in=[Quote.STATUS_SENT,
        Quote.STATUS_ACCEPTED, Quote.STATUS_DRAFT]).aggregate(t=Sum("total", default=0))["t"]
    return render(request, "merchant/quotes.html", _merchant_context(request, store, page=page,
        status=status, statuses=Quote.STATUS_CHOICES, open_total=open_total,
        can_edit=_can_edit(request, store)))


def _doc_form_context(request, store, **extra):
    return _merchant_context(request, store,
        products=Product.objects.filter(store=store, is_active=True).order_by("name"),
        customers=Customer.objects.filter(store=store).order_by("name")[:500],
        today=timezone.localdate().isoformat(), post=_posted(request), **extra)


@login_required(login_url=LOGIN)
def merchant_quote_edit(request, quote_id=None):
    store = _store(request, EDIT_ROLES)
    quote = get_object_or_404(Quote, pk=quote_id, store=store) if quote_id else None
    if quote and quote.status == Quote.STATUS_INVOICED:
        messages.error(request, "Penawaran yang sudah dijadikan invoice tidak bisa diubah.")
        return redirect("merchant-quote-detail", quote.pk)
    if request.method == "POST":
        try:
            customer = _customer(store, request.POST.get("customer_id"))
            valid_until = _date(request.POST.get("valid_until"))
            quote = quotes.save_quote(store, quote=quote, lines=_product_lines(request, store),
                buyer_name=request.POST.get("buyer_name") or (customer.name if customer else ""),
                customer=customer, issued_on=_date(request.POST.get("issued_on")),
                valid_until=valid_until, company_name=request.POST.get("company_name", ""),
                buyer_npwp=request.POST.get("buyer_npwp", ""), phone=request.POST.get("phone", ""),
                address=request.POST.get("address", ""), notes=request.POST.get("notes", ""),
                discount=_amount(request.POST.get("discount")),
                shipping=_amount(request.POST.get("shipping")), actor=request.user)
        except (quotes.QuoteError, InvoiceError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Isian tidak valid.")
        else:
            _audit(request, store, "quote.saved", quote, total=str(quote.total))
            messages.success(request, f"Penawaran {quote.number} disimpan.")
            return redirect("merchant-quote-detail", quote.pk)
    prefill = _customer(store, request.GET.get("customer"))
    return render(request, "merchant/quote_form.html", _doc_form_context(request, store,
        quote=quote, prefill=prefill or defaultdict(str), kind="quote", items=quote.items.all() if quote else []))


@login_required(login_url=LOGIN)
def merchant_quote_detail(request, quote_id):
    store = _store(request)
    quote = get_object_or_404(Quote.objects.select_related("invoice", "customer"), pk=quote_id,
        store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action")
        try:
            if action == "convert":
                invoice = quotes.convert_to_invoice(quote,
                    on_credit=request.POST.get("paid") != "1",
                    due_days=int(request.POST.get("due_days") or 14), actor=request.user)
                _audit(request, store, "quote.invoiced", quote, invoice=invoice.number)
                messages.success(request, f"Invoice {invoice.number} dibuat dari penawaran.")
                return redirect("merchant-invoice-detail", invoice_id=invoice.id)
            if action in {"sent", "accepted", "declined", "draft"}:
                quotes.set_status(quote, action)
                messages.success(request, "Status penawaran diperbarui.")
        except (quotes.QuoteError, ValueError) as error:
            messages.error(request, str(error))
        return redirect("merchant-quote-detail", quote.pk)
    return render(request, "merchant/quote_detail.html", _merchant_context(request, store,
        quote=quote, items=quote.items.select_related("product"), audit=audit_trail(store, quote),
        expired=quotes.is_expired(quote), can_edit=_can_edit(request, store)))


# ---------------------------------------------------------------------------
# Invoice berulang
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_recurring(request):
    store = _store(request)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        item = get_object_or_404(RecurringInvoice, pk=request.POST.get("id"), store=store)
        action = request.POST.get("action")
        if action == "toggle":
            item.is_active = not item.is_active
            item.save(update_fields=["is_active"])
        elif action == "run":
            issued = recurring.run_one(item)
            messages.success(request, f"{len(issued)} invoice diterbitkan." if issued
                else "Belum ada invoice yang jatuh tempo.")
        return redirect("merchant-recurring")
    rows = list(RecurringInvoice.objects.filter(store=store).prefetch_related("items"))
    today = timezone.localdate()
    for row in rows:
        row.per_run = sum((item.quantity * item.unit_price for item in row.items.all()),
            Decimal(0)) - row.discount + row.shipping
        row.due = row.is_active and row.next_run <= today
    return render(request, "merchant/recurring.html", _merchant_context(request, store, rows=rows,
        can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_recurring_edit(request, recurring_id=None):
    store = _store(request, EDIT_ROLES)
    template = get_object_or_404(RecurringInvoice, pk=recurring_id, store=store) \
        if recurring_id else None
    if request.method == "POST":
        try:
            customer = _customer(store, request.POST.get("customer_id"))
            template = recurring.save_recurring(store, recurring=template,
                name=request.POST.get("name", ""), lines=_product_lines(request, store),
                buyer_name=request.POST.get("buyer_name") or (customer.name if customer else ""),
                customer=customer, interval=request.POST.get("interval", "month"),
                next_run=_date(request.POST.get("next_run"), timezone.localdate()),
                end_date=_date(request.POST.get("end_date")),
                company_name=request.POST.get("company_name", ""),
                buyer_npwp=request.POST.get("buyer_npwp", ""), phone=request.POST.get("phone", ""),
                address=request.POST.get("address", ""), notes=request.POST.get("notes", ""),
                discount=_amount(request.POST.get("discount")),
                shipping=_amount(request.POST.get("shipping")),
                due_days=int(request.POST.get("due_days") or 14), actor=request.user)
        except (recurring.RecurringError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Isian tidak valid.")
        else:
            _audit(request, store, "recurring.saved", template)
            messages.success(request, "Jadwal invoice berulang disimpan.")
            return redirect("merchant-recurring")
    prefill = _customer(store, request.GET.get("customer"))
    return render(request, "merchant/quote_form.html", _doc_form_context(request, store,
        quote=template, prefill=prefill or defaultdict(str), kind="recurring", intervals=RecurringInvoice.INTERVALS,
        items=template.items.all() if template else []))



@login_required(login_url=LOGIN)
def merchant_delivery_note(request, invoice_id):
    """Surat jalan: what is in the parcel, to whom, with signature boxes. No prices."""
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "finance", "warehouse", "viewer"})
    invoice = get_object_or_404(Invoice.objects.select_related("order", "store"), pk=invoice_id,
        store=store)
    if invoice.status in {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}:
        raise Http404
    order = invoice.order
    return render(request, "tax/delivery_note.html", {"invoice": invoice, "order": order,
        "items": invoice.items.all(), "store": store, "today": timezone.localdate(),
        "address": (order.full_address if order.shipping_address not in {"", "-"} else
            invoice.buyer_address)})


@login_required(login_url=LOGIN)
def merchant_invoice_style(request):
    """Tampilan invoice: the logo, colour and footer on every document sent to a buyer."""
    from django import forms
    from django.core.files.uploadedfile import UploadedFile

    from . import branding
    from .models import TaxProfile

    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "finance"})
    profile, _created = TaxProfile.objects.get_or_create(store=store)

    class StyleForm(forms.ModelForm):
        class Meta:
            model = TaxProfile
            fields = ["invoice_logo", "invoice_show_name", "invoice_accent", "invoice_footer"]
            widgets = {"invoice_footer": forms.Textarea(attrs={"rows": 4, "maxlength": 600})}

        def clean_invoice_logo(self):
            upload = self.cleaned_data.get("invoice_logo")
            if isinstance(upload, UploadedFile):  # a new file; a kept one was checked when saved
                branding.validate_logo(upload)
            return upload

    form = StyleForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            _audit(request, store, "invoice_style.updated", profile)
            messages.success(request, "Tampilan invoice disimpan. Semua invoice, surat tagihan, dan "
                "surat jalan memakai tampilan ini.")
            return redirect("merchant-invoice-style")
        messages.error(request, "Tampilan belum disimpan: periksa isian yang bertanda merah.")
    return render(request, "merchant/invoice_style.html", _merchant_context(request, store,
        form=form, brand=branding.brand(store), store_logo=bool(store.logo)))
