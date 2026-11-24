"""Penggajian: employees, the monthly payroll run, paying it, BPJS, and the payslip."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail

from . import approvals, idempotency, payroll
from .bank import bank_accounts
from .ledger import entries_for
from .models import BankAccount, Employee, PayrollRun

LOGIN = "/login/"
VIEW_ROLES, EDIT_ROLES = permissions.roles("payroll.view"), permissions.roles("payroll.edit")
SETTING_FIELDS = ["kes_company", "kes_employee", "kes_cap", "jht_company", "jht_employee", "jkk", "jkm",
    "jp_company", "jp_employee", "jp_cap"]


def _date(raw, default=None):
    try:
        return datetime.date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


def _number(raw):
    """``1.250.000`` (rupiah) or ``3,7`` (a rate) → Decimal."""
    raw = (raw or "").strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1 or (raw.count(".") == 1 and len(raw.rsplit(".", 1)[1]) == 3):
        raw = raw.replace(".", "")
    return Decimal(raw or "0")


def _bank(store, raw):
    return BankAccount.objects.filter(store=store, pk=raw).first() if raw and str(raw).isdigit() else None


def _can_edit(request, store):
    return permissions.can(request.user, store, "payroll.edit")


@login_required(login_url=LOGIN)
def merchant_payroll(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if request.POST.get("action") == "settings":
            settings = payroll.settings_for(store)
            try:
                for name in SETTING_FIELDS:
                    value = _number(request.POST.get(name))
                    if value < 0 or (not name.endswith("_cap") and value > 20):
                        raise InvalidOperation
                    setattr(settings, name, value)
            except (InvalidOperation, ValueError):
                messages.error(request, "Tarif BPJS 0 sampai 20% dan batas upah tidak boleh negatif.")
            else:
                settings.save()
                audit_log(store, request.user, "payroll.settings_updated", settings)
                messages.success(request, "Tarif BPJS disimpan. Berlaku untuk penggajian yang disusun berikutnya.")
            return redirect("merchant-payroll")
        month = request.POST.get("period", "")
        try:
            period = datetime.date.fromisoformat(f"{month}-01")
            run = payroll.create_run(store, period, actor=request.user)
        except ValueError as error:
            messages.error(request, str(error) if isinstance(error, payroll.PayrollError) else "Pilih bulan.")
        else:
            messages.success(request, f"Penggajian {period:%m/%Y} disusun dari data karyawan. Periksa lalu posting.")
            return redirect("merchant-payroll-run", run_id=run.pk)
        return redirect("merchant-payroll")
    runs = list(PayrollRun.objects.filter(store=store).prefetch_related("lines"))
    wages, bpjs = payroll.owed(store)
    from .models import Withholding
    pph = sum((w.amount for w in Withholding.objects.filter(store=store, kind="pph21", deposit__isnull=True)),
        Decimal(0))
    today = timezone.localdate()
    live = [run for run in runs if run.status != PayrollRun.VOID]
    return render(request, "merchant/payroll.html", _merchant_context(request, store, runs=runs, wages=wages,
        bpjs=bpjs, pph=pph, latest=live[0] if live else None,
        employees=Employee.objects.filter(store=store, is_active=True).count(),
        settings=payroll.settings_for(store), setting_fields=SETTING_FIELDS,
        next_period=(live[0].period + datetime.timedelta(days=32)).strftime("%Y-%m") if live else today.strftime("%Y-%m"),
        can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_payroll_employees(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    post = {}
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        employee = None
        if request.POST.get("employee_id"):
            employee = get_object_or_404(Employee, pk=request.POST["employee_id"], store=store)
        try:
            saved = payroll.employee_from_form(store, request.POST, employee)
        except (payroll.PayrollError, ValueError) as error:
            messages.error(request, str(error) if isinstance(error, payroll.PayrollError) else "Tanggal tidak terbaca.")
            post = request.POST
        else:
            audit_log(store, request.user, "employee.updated" if employee else "employee.created", saved,
                name=saved.name)
            messages.success(request, f"{saved.name} {'diperbarui' if employee else 'ditambahkan'}.")
            return redirect("merchant-payroll-employees")
    employees = list(Employee.objects.filter(store=store).order_by("-is_active", "name"))
    active = [e for e in employees if e.is_active]
    return render(request, "merchant/payroll_employees.html", _merchant_context(request, store,
        employees=employees, active=len(active), monthly=sum((e.base_salary + e.allowance for e in active), Decimal(0)),
        ptkp=Employee._meta.get_field("ptkp_status").choices, post=post, can_edit=_can_edit(request, store)))


def _rows(post, run):
    rows = {}
    for line in run.lines.all():
        key = str(line.pk)
        if f"salary_{key}" not in post:
            continue
        rows[line.pk] = {"salary": _number(post.get(f"salary_{key}")),
            "allowance": _number(post.get(f"allowance_{key}")), "extra": _number(post.get(f"extra_{key}")),
            "pph21_manual": _number(post[f"rate_{key}"]) if post.get(f"rate_{key}", "").strip() else None,
            "remove": post.get(f"remove_{key}") == "1"}
    return rows


@login_required(login_url=LOGIN)
def merchant_payroll_run(request, run_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    run = get_object_or_404(PayrollRun, pk=run_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action, today = request.POST.get("action", ""), timezone.localdate()
        key = request.POST.get("idem", "")
        if action in {"pay", "pay_bpjs"} and idempotency.claim(store, f"payroll_{action}", key) is not None:
            messages.info(request, "Sudah dicatat.")
            return redirect("merchant-payroll-run", run_id=run.pk)
        try:
            if action == "save":
                payroll.update_lines(run, _rows(request.POST, run), actor=request.user)
                messages.success(request, "Penggajian dihitung ulang.")
            elif action == "add_missing":
                added = payroll.add_missing_employees(run, actor=request.user)
                messages.success(request, f"{added} karyawan ditambahkan." if added else "Semua karyawan aktif sudah ada.")
            elif action == "post":
                payroll.update_lines(run, _rows(request.POST, run), actor=request.user)
                run.refresh_from_db()
                if approvals.required(store, "payroll", run.gross, request.user):
                    approvals.submit(store, "payroll", run.gross, f"Posting penggajian {run.period:%m/%Y} ({run.number})",
                        {"run_id": str(run.pk)}, request.user, subject=run)
                    PayrollRun.objects.filter(pk=run.pk).update(approval_pending=True)
                    messages.info(request, "Penggajian di atas batas persetujuan: menunggu persetujuan orang lain.")
                else:
                    payroll.post_run(run, actor=request.user)
                    messages.success(request, "Penggajian diposting: gaji, BPJS, dan PPh 21 masuk buku besar.")
            elif action == "pay":
                payroll.pay_run(run, date=_date(request.POST.get("date"), today),
                    bank_account=_bank(store, request.POST.get("bank_account")), actor=request.user)
                idempotency.finish(store, "payroll_pay", key, run.pk)
                messages.success(request, "Gaji dibayar.")
            elif action == "pay_bpjs":
                payroll.pay_bpjs(run, date=_date(request.POST.get("date"), today),
                    bank_account=_bank(store, request.POST.get("bank_account")), actor=request.user)
                idempotency.finish(store, "payroll_pay_bpjs", key, run.pk)
                messages.success(request, "BPJS disetor.")
            elif action == "void":
                payroll.void_run(run, actor=request.user)
                messages.success(request, "Penggajian dibatalkan.")
        except (payroll.PayrollError, InvalidOperation, ValueError) as error:
            if action in {"pay", "pay_bpjs"}:
                idempotency.release(store, f"payroll_{action}", key)
            messages.error(request, f"Gagal: {error}" if str(error) else "Angka tidak terbaca.")
        return redirect("merchant-payroll-run", run_id=run.pk)
    lines = list(run.lines.select_related("employee"))
    sources = [("payroll", run.pk), ("payroll_pay", run.pk), ("payroll_bpjs", run.pk)]
    return render(request, "merchant/payroll_run.html", _merchant_context(request, store, run=run, lines=lines,
        journal=entries_for(store, sources), banks=bank_accounts(store), audit=audit_trail(store, run),
        today=timezone.localdate().isoformat(), idem_key=idempotency.new_key(), can_edit=_can_edit(request, store),
        missing=Employee.objects.filter(store=store, is_active=True).exclude(
            pk__in=[line.employee_id for line in lines]).count() if run.status == PayrollRun.DRAFT else 0))


@login_required(login_url=LOGIN)
def merchant_payroll_slip(request, run_id, line_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    run = get_object_or_404(PayrollRun, pk=run_id, store=store)
    line = get_object_or_404(run.lines.select_related("employee"), pk=line_id)
    return render(request, "merchant/payroll_slip.html", {"store": store, "run": run, "line": line,
        "slip": payroll.slip(line, payroll.settings_for(store)), "merchant_store": store})

