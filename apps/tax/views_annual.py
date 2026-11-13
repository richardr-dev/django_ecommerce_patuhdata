"""SPT Tahunan worksheet and the tax calendar."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log

from . import fiscal, tax_calendar
from .models import FiscalAdjustment, TaxFiling

LOGIN = "/merchant/login/"
VIEW_ROLES, EDIT_ROLES = permissions.roles("tax.view"), permissions.roles("tax.edit")


def _number(raw):
    return Decimal((raw or "0").replace(".", "").replace(",", ".") or "0")


@login_required(login_url=LOGIN)
def merchant_spt_tahunan(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year") or request.POST.get("year") or today.year - 1)
    except ValueError:
        year = today.year - 1
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        try:
            if request.POST.get("action") == "delete":
                item = get_object_or_404(FiscalAdjustment, pk=request.POST.get("id"), store=store, year=year)
                item.delete()
                audit_log(store, request.user, "fiscal_adjustment.deleted", store, year=year, description=item.description)
                messages.success(request, "Baris dihapus.")
            else:
                amount = _number(request.POST.get("amount"))
                description = request.POST.get("description", "").strip()[:200]
                kind = request.POST.get("kind")
                if kind not in dict(FiscalAdjustment.KINDS) or not description or amount <= 0:
                    raise ValueError("Isi jenis, keterangan, dan jumlah lebih dari nol.")
                item = FiscalAdjustment.objects.create(store=store, year=year, kind=kind, description=description, amount=amount,
                    created_by=request.user)
                audit_log(store, request.user, "fiscal_adjustment.created", store, year=year, kind=kind, amount=amount)
                messages.success(request, "Baris ditambahkan ke lembar kerja.")
        except (ValueError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect(f"{request.path}?year={year}")
    sheet = fiscal.worksheet(store, year)
    return render(request, "merchant/spt_tahunan.html", _merchant_context(request, store, w=sheet, year=year,
        years=range(today.year - 4, today.year + 1), kinds=FiscalAdjustment.KINDS, can_edit=permissions.can(request.user, store, "tax.edit"),
        facility_note=fiscal.DISCOUNT_BASE))


@login_required(login_url=LOGIN)
def merchant_tax_calendar(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    today = timezone.localdate()
    try:
        year, month = (int(x) for x in (request.GET.get("month") or "").split("-"))
        datetime.date(year, month, 1)
    except ValueError:
        year, month = today.year, today.month
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        code = request.POST.get("code", "")[:40]
        try:
            if request.POST.get("action") == "undo":
                TaxFiling.objects.filter(store=store, code=code).delete()
                messages.success(request, "Tanda selesai dihapus.")
            else:
                done_on = datetime.date.fromisoformat(request.POST.get("done_on") or today.isoformat())
                TaxFiling.objects.update_or_create(store=store, code=code, defaults={"done_on": done_on, "done_by": request.user,
                    "reference": request.POST.get("reference", "")[:80]})
                audit_log(store, request.user, "tax_filing.done", store, code=code)
                messages.success(request, "Ditandai sudah dilakukan.")
        except ValueError as error:
            messages.error(request, f"Gagal: {error}")
        return redirect(f"{request.path}?month={year}-{month:02d}")
    rows = tax_calendar.obligations(store, year, month, today)
    prev = (year - 1, 12) if month == 1 else (year, month - 1)
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return render(request, "merchant/tax_calendar.html", _merchant_context(request, store, rows=rows, year=year, month=month,
        month_date=datetime.date(year, month, 1), prev=f"{prev[0]}-{prev[1]:02d}", next=f"{nxt[0]}-{nxt[1]:02d}",
        today=today.isoformat(), can_edit=permissions.can(request.user, store, "tax.edit"),
        counts={s: sum(1 for r in rows if r["status"] == s) for s in ("overdue", "soon", "upcoming", "done")}))
