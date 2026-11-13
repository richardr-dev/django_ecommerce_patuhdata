"""Pinjaman: list, new loan, schedule and paying an installment."""
import datetime
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

from . import idempotency, loans
from .bank import bank_accounts
from .ledger import entries_for
from .models import Loan, LoanInstallment

LOGIN = "/merchant/login/"
EDIT_ROLES, VIEW_ROLES = permissions.roles("books.edit"), permissions.roles("books.view")


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


def _date(raw, default=None):
    try:
        return datetime.date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


@login_required(login_url=LOGIN)
def merchant_loans(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, "loan", key)
        if earlier is not None:
            messages.info(request, "Pinjaman ini sudah disimpan.")
            return redirect("merchant-loan", loan_id=earlier) if earlier else redirect("merchant-loans")
        try:
            loan = loans.create_loan(store, lender=request.POST.get("lender", ""), principal=_number(request.POST.get("principal")),
                annual_rate=_number(request.POST.get("annual_rate")), term_months=int(request.POST.get("term_months") or 0),
                start_date=_date(request.POST.get("start_date"), today), method=request.POST.get("method", "annuity"),
                received_into_code=request.POST.get("received_into", ""), notes=request.POST.get("notes", ""), actor=request.user)
        except (loans.LoanError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "loan", key)
            messages.error(request, f"Gagal: {error}")
        else:
            idempotency.finish(store, "loan", key, loan.pk)
            audit_log(store, request.user, "loan.created", loan, principal=loan.principal)
            messages.success(request, "Pinjaman dicatat. Jadwal angsuran dibuat; bayar tiap angsuran dari halaman ini.")
            return redirect("merchant-loan", loan_id=loan.pk)
    rows = list(Loan.objects.filter(store=store).prefetch_related("installments"))
    for loan in rows:
        loan.rest = loans.outstanding(loan)
        loan.next = next((i for i in loan.installments.all() if not i.paid_on), None)
    return render(request, "merchant/loans.html", _merchant_context(request, store, rows=rows, today=today.isoformat(),
        owed=sum((loan.rest for loan in rows), Decimal(0)), late=loans.overdue(store, today).count(),
        banks=bank_accounts(store), idem_key=idempotency.new_key(), can_edit=permissions.can(request.user, store, "books.edit")))


@login_required(login_url=LOGIN)
def merchant_loan(request, loan_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    loan = get_object_or_404(Loan, pk=loan_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        try:
            item = get_object_or_404(LoanInstallment, pk=request.POST.get("installment"), loan=loan)
            loans.pay_installment(item, paid_on=_date(request.POST.get("paid_on"), timezone.localdate()),
                paid_from_code=request.POST.get("paid_from", ""), actor=request.user)
            audit_log(store, request.user, "loan.installment_paid", loan, number=item.number, amount=item.amount)
            messages.success(request, f"Angsuran {item.number} dibayar: pokok, bunga, dan kas tercatat.")
        except (loans.LoanError, ValueError, Http404) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("merchant-loan", loan_id=loan.pk)
    items = list(loan.installments.all())
    sources = [("loan", loan.pk)] + [("loan_payment", f"{loan.pk}:{i.number}") for i in items if i.paid_on]
    return render(request, "merchant/loan_detail.html", _merchant_context(request, store, loan=loan, items=items,
        rest=loans.outstanding(loan), paid_interest=sum((i.interest for i in items if i.paid_on), Decimal(0)),
        total_interest=sum((i.interest for i in items), Decimal(0)), journal=entries_for(store, sources),
        banks=bank_accounts(store), today=timezone.localdate().isoformat(), audit=audit_trail(store, loan),
        next_due=next((i for i in items if not i.paid_on), None), can_edit=permissions.can(request.user, store, "books.edit")))
