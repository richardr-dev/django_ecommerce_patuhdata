"""Nota debit (retur ke supplier) and paying several bills of one supplier at once."""
import datetime
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import approvals, idempotency, supplier_notes
from .bank import bank_accounts
from .ledger import entries_for
from .models import Bill, DebitNote, Supplier
from .payables import open_bills

LOGIN = "/login/"
EDIT_ROLES = permissions.roles("books.edit")
VIEW_ROLES = permissions.roles("books.view")


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


@login_required(login_url=LOGIN)
def merchant_debit_notes(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    rows = DebitNote.objects.filter(store=store).select_related("supplier", "bill")[:300]
    return render(request, "merchant/debit_notes.html", _merchant_context(request, store, rows=rows))


@login_required(login_url=LOGIN)
def merchant_debit_note_new(request):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    try:
        bill = get_object_or_404(Bill.objects.select_related("supplier"), pk=_uuid.UUID(str(request.GET.get("bill") or request.POST.get("bill"))), store=store)
    except (ValueError, TypeError):
        raise Http404 from None
    items = list(bill.items.select_related("product"))
    for item in items:
        item.max_return = supplier_notes.returnable(item)
        item.is_goods = bool(item.product_id)
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "debit_note", key)
        if earlier is not None:
            messages.info(request, "Nota debit ini sudah disimpan.")
            return redirect("merchant-bill-detail", bill_id=bill.pk)
        try:
            note = supplier_notes.create_debit_note(bill, reason=request.POST.get("reason", ""), actor=request.user,
                quantities={item.pk: _number(request.POST.get(f"qty_{item.pk}")) for item in items},
                note_date=datetime.date.fromisoformat(request.POST.get("note_date") or timezone.localdate().isoformat()))
        except (supplier_notes.NoteError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "debit_note", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "debit_note", key, note.pk)
            audit_log(store, request.user, "debit_note.created", note, bill=bill.pk, total=note.total)
            messages.success(request, f"Nota debit {note.number} dibuat. Utang ke {bill.supplier.name} berkurang {note.total:,.0f}.".replace(",", "."))
            return redirect("merchant-debit-note", note_id=note.pk)
    return render(request, "merchant/debit_note_new.html", _merchant_context(request, store, bill=bill, items=items,
        today=timezone.localdate().isoformat(), idem_key=idempotency.new_key(), post=request.POST))


@login_required(login_url=LOGIN)
def merchant_debit_note(request, note_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        note = get_object_or_404(DebitNote.objects.select_related("supplier", "bill"), pk=_uuid.UUID(str(note_id)), store=store)
    except ValueError:
        raise Http404 from None
    return render(request, "merchant/debit_note_detail.html", _merchant_context(request, store, note=note,
        items=list(note.items.all()), journal=entries_for(store, [("debit_note", note.pk)]), audit=audit_trail(store, note)))


@login_required(login_url=LOGIN)
def merchant_supplier_pay(request, supplier_id):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    supplier = get_object_or_404(Supplier, pk=supplier_id, store=store)
    bills = open_bills(store, supplier_id=supplier.pk)
    if request.method == "POST":
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "supplier_pay", key)
        if earlier is not None:
            messages.info(request, "Pembayaran ini sudah disimpan.")
            return redirect("merchant-supplier-detail", supplier_id=supplier.pk)
        try:
            allocations = [(bill, _number(request.POST.get(f"amount_{bill.pk}"))) for bill in bills]
            total = sum((amount for _, amount in allocations), Decimal(0))
            if approvals.required(store, "bill_payment", total, request.user):
                raise supplier_notes.NoteError("Nilainya besar dan perlu persetujuan orang kedua. Bayar satu per satu "
                    "dari halaman tagihan agar setiap pembayaran diajukan dan disetujui.")
            for bill, amount in allocations:
                if amount > 0 and permissions.maker_may_not_check(store, request.user, bill.created_by, "books.edit"):
                    raise supplier_notes.NoteError(f"Tagihan {bill.number or str(bill.pk)[:8]} Anda buat sendiri; minta "
                        "admin atau keuangan lain yang membayarnya.")
            bank = next((b for b in bank_accounts(store) if str(b.pk) == request.POST.get("bank_account")), None)
            payments = supplier_notes.pay_bills(allocations, bank_account=bank, actor=request.user,
                method=request.POST.get("method", "transfer"), reference=request.POST.get("reference", "")[:120],
                paid_on=datetime.date.fromisoformat(request.POST.get("paid_on") or timezone.localdate().isoformat()))
        except (supplier_notes.NoteError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "supplier_pay", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "supplier_pay", key, supplier.pk)
            for payment in payments:
                audit_log(store, request.user, "bill.paid", payment.bill, amount=payment.amount, together=len(payments))
            messages.success(request, f"{len(payments)} tagihan dibayar sekaligus.")
            return redirect("merchant-supplier-detail", supplier_id=supplier.pk)
    return render(request, "merchant/supplier_pay.html", _merchant_context(request, store, supplier=supplier, bills=bills,
        banks=bank_accounts(store), today=timezone.localdate().isoformat(), idem_key=idempotency.new_key(),
        methods=Bill.objects.none().model._meta.apps.get_model("accounting", "BillPayment").METHOD_CHOICES, post=request.POST))
