"""Jurnal penyesuaian: prepaid expenses, accruals and recurring journals, and the receivable provision."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import adjustments, idempotency
from .bank import bank_accounts
from .ledger import chart_of_accounts
from .models import EXPENSE_CATEGORIES, JournalTemplate, PrepaidExpense

LOGIN = "/merchant/login/"
EDIT_ROLES = permissions.roles("books.edit")
VIEW_ROLES = permissions.roles("books.view")
TABS = [("prepaid", "Dibayar di muka"), ("accrual", "Akrual & berulang"), ("provision", "Cadangan piutang")]


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


def _date(raw, default=None):
    try:
        return datetime.date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


@login_required(login_url=LOGIN)
def merchant_adjustments(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    tab = request.GET.get("tab", "prepaid") if request.GET.get("tab") in dict(TABS) else "prepaid"
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action, tab = request.POST.get("action", ""), request.POST.get("tab", tab)
        key = request.POST.get("idem", "")
        earlier = idempotency.claim(store, f"adjust_{action}", key) if action.startswith("add") or action == "provision" else None
        if earlier is not None:
            messages.info(request, "Isian ini sudah disimpan.")
            return redirect(f"{request.path}?tab={tab}")
        try:
            if action == "add_prepaid":
                item = adjustments.add_prepaid(store, name=request.POST.get("name", ""), actor=request.user,
                    start_date=_date(request.POST.get("start_date"), today), months=int(request.POST.get("months") or 0),
                    total=_number(request.POST.get("total")), expense_code=request.POST.get("expense_code", ""),
                    paid_from_code=request.POST.get("paid_from", ""), notes=request.POST.get("notes", ""))
                audit_log(store, request.user, "prepaid.created", item, total=item.total, months=item.months)
                messages.success(request, f"{item.name} dicatat. Amortisasi bulanan dibukukan otomatis tiap akhir bulan.")
            elif action in ("add_accrual", "add_recurring"):
                kind = JournalTemplate.ACCRUAL if action == "add_accrual" else JournalTemplate.RECURRING
                template = adjustments.add_template(store, kind=kind, name=request.POST.get("name", ""), actor=request.user,
                    debit_code=request.POST.get("debit_code", ""), credit_code=request.POST.get("credit_code", ""),
                    amount=_number(request.POST.get("amount")), first_date=_date(request.POST.get("first_date"), today),
                    frequency=request.POST.get("frequency", "once"), auto_reverse=request.POST.get("auto_reverse") == "1",
                    end_on=_date(request.POST.get("end_on")), today=today)
                audit_log(store, request.user, "journal_template.created", template, amount=template.amount, kind=kind)
                messages.success(request, f"{template.name} disimpan." + (" Pembalikan otomatis di awal periode berikutnya." if template.auto_reverse else ""))
            elif action == "stop_template":
                template = JournalTemplate.objects.get(pk=request.POST.get("id"), store=store)
                adjustments.stop_template(template)
                messages.success(request, f"{template.name} dihentikan. Jurnal yang sudah dibukukan tidak berubah.")
            elif action == "end_prepaid":
                item = PrepaidExpense.objects.get(pk=request.POST.get("id"), store=store)
                adjustments.end_prepaid(item)
                messages.success(request, f"Amortisasi {item.name} dihentikan. Sisa yang belum dibebankan tetap di Biaya dibayar di muka sampai Anda membuat jurnalnya.")
            elif action == "provision":
                rates = {k: _number(request.POST.get(f"rate_{k}")) for k in adjustments.DEFAULT_RATES}
                entry = adjustments.book_provision(store, as_of=_date(request.POST.get("as_of"), today), rates=rates, actor=request.user)
                audit_log(store, request.user, "provision.booked", entry, as_of=request.POST.get("as_of", ""))
                messages.success(request, "Cadangan kerugian piutang dicatat.")
        except (adjustments.AdjustmentError, InvalidOperation, ValueError, PrepaidExpense.DoesNotExist,
                JournalTemplate.DoesNotExist) as error:
            if action.startswith("add") or action == "provision":
                idempotency.release(store, f"adjust_{action}", key)
            messages.error(request, f"Gagal: {error}")
        else:
            if action.startswith("add") or action == "provision":
                idempotency.finish(store, f"adjust_{action}", key, "ok")
        return redirect(f"{request.path}?tab={tab}")

    accounts = sorted(chart_of_accounts(store).values(), key=lambda a: a.code)
    context = dict(tab=tab, tabs=TABS, today=today.isoformat(), can_edit=permissions.can(request.user, store, "books.edit"),
        accounts=accounts, expense_accounts=[a for a in accounts if a.code in dict(EXPENSE_CATEGORIES)],
        pay_accounts=[a for a in accounts if a.type in ("asset", "liability") and (a.code.startswith("11") or a.code == "2300")],
        banks=bank_accounts(store), idem_key=idempotency.new_key())
    if tab == "prepaid":
        items = list(PrepaidExpense.objects.filter(store=store).select_related("paid_from"))
        for item in items:
            item.done = adjustments.amortized(item)
            item.rest = item.total - item.done
            item.pct = int(item.done * 100 / item.total) if item.total else 0
        context["prepaids"] = items
        context["prepaid_rest"] = sum((i.rest for i in items if i.status == "active"), Decimal(0))
    elif tab == "accrual":
        context["templates"] = JournalTemplate.objects.filter(store=store)
    else:
        as_of = _date(request.GET.get("as_of"), today)
        rates = {k: _number(request.GET.get(f"rate_{k}")) for k in adjustments.DEFAULT_RATES if request.GET.get(f"rate_{k}")}
        context.update(plan=adjustments.provision_needed(store, as_of, rates), as_of=as_of)
    return render(request, "merchant/adjustments.html", _merchant_context(request, store, **context))
