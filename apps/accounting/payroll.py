"""Penggajian: a month's salaries, BPJS and PPh 21, posted to the ledger and paid from a bank account.

A run is drafted from the active employees (gaji pokok, tunjangan tetap, their TER rate), edited per
line (lembur, bonus, THR in ``extra``), then posted on the last day of the month:

    Gaji & upah 6200            gross                 Utang BPJS 2160     employee + company BPJS
    Beban BPJS perusahaan 6210  company BPJS          Utang PPh 2150      PPh 21
                                                      Utang gaji 2170     take-home pay

Paying the run clears Utang gaji from the bank; "Setor BPJS" clears Utang BPJS; PPh 21 becomes one
bukti potong per employee in the PPh register (``withholding``), deposited from Setor PPh like any other.

BPJS: Kesehatan and JP are capped at the store's wage caps; the base is gaji pokok + tunjangan tetap.
PPh 21 (``accounting/pph21.py``): January to November, the TER rate of the employee's category for the
month's gross (gross + the company's Kesehatan, JKK and JKM premiums) × that gross; a rate typed on the
employee or the line overrides the table. December: the year is recomputed with the Pasal 17 rates (biaya
jabatan, the employee's JHT + JP, PTKP) and what the year's earlier runs withheld is subtracted; a negative
result is returned to the employee. Every employee is treated as a permanent employee (pegawai tetap)."""
import calendar
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q

from apps.shops.audit import log as audit_log

from . import pph21, withholding
from .ledger import (
    BPJS_EXPENSE,
    BPJS_PAYABLE,
    CASH,
    SALARIES,
    WAGES_PAYABLE,
    WITHHELD,
    LedgerError,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from .models import Employee, PayrollLine, PayrollRun, PayrollSettings, Withholding

ZERO = Decimal(0)


class PayrollError(ValueError):
    pass


def _rp(value):
    return Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def settings_for(store):
    return PayrollSettings.objects.filter(store=store).first() or PayrollSettings(store=store)


def month_end(period):
    return period.replace(day=calendar.monthrange(period.year, period.month)[1])


def default_post_date(period, today=None):
    """A past month posts on its last day; the current month on the day it is posted, so salaries paid
    before the month ends (the 25th) are never paid before they were booked."""
    from django.utils import timezone
    today = today or timezone.localdate()
    end = month_end(period)
    return today if period <= today < end else end


def rate_text(rate):
    """3.00 → "3", 0.25 → "0,25"."""
    text = f"{Decimal(rate):f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text).replace(".", ",")


# ── Working out one line ────────────────────────────────────────────────────

def bpjs_parts(wage, settings):
    """Each BPJS premium on a monthly ``wage``: ``{name: (company, employee)}``."""
    kes_base, jp_base = min(wage, settings.kes_cap), min(wage, settings.jp_cap)
    return {
        "BPJS Kesehatan": (_rp(kes_base * settings.kes_company / 100), _rp(kes_base * settings.kes_employee / 100)),
        "JHT": (_rp(wage * settings.jht_company / 100), _rp(wage * settings.jht_employee / 100)),
        "JKK": (_rp(wage * settings.jkk / 100), ZERO),
        "JKM": (_rp(wage * settings.jkm / 100), ZERO),
        "JP": (_rp(jp_base * settings.jp_company / 100), _rp(jp_base * settings.jp_employee / 100)),
    }


TAXABLE_PREMIUMS = ("BPJS Kesehatan", "JKK", "JKM")  # paid by the company, counted as the employee's income
PENSION = ("JHT", "JP")  # paid by the employee, deducted from the year's income in December


def year_end(line):
    """The December recalculation for ``line``: the employee's earlier lines this year in runs that stand,
    plus this one."""
    earlier = list(PayrollLine.objects.filter(employee=line.employee, run__store=line.run.store,
        run__period__year=line.run.period.year, run__status__in=[PayrollRun.POSTED, PayrollRun.PAID]
        ).exclude(run=line.run))
    return pph21.annual(line.employee.ptkp_status,
        gross=sum((row.pph21_base for row in earlier), ZERO) + line.pph21_base,
        pension=sum((row.pension_employee for row in earlier), ZERO) + line.pension_employee,
        months=len(earlier) + 1, withheld=sum((row.pph21 for row in earlier), ZERO))


def leaves_in(employee, period):
    """True when ``period`` (a month) is the month the employee stops working."""
    left = employee.left_on
    return bool(left) and (left.year, left.month) == (period.year, period.month)


def last_period(line):
    """December, or the month the employee leaves: the masa pajak terakhir of PMK 168/2023."""
    return line.run.period.month == 12 or leaves_in(line.employee, line.run.period)


def pph21_label(line):
    if line.pph21_method == "annual":
        if line.run.period.month == 12:
            return "PPh 21 Desember (hitung ulang setahun)"
        return "PPh 21 masa terakhir (berhenti bekerja)"
    if line.pph21_method == "manual":
        return f"PPh 21 (tarif {rate_text(line.pph21_rate)}%)"
    return f"PPh 21 (TER {line.employee.ter_category} {rate_text(line.pph21_rate)}%)"


def compute_line(line, settings):
    """Fill the BPJS, PPh 21 and take-home figures of ``line`` from its salary, allowance and extra."""
    if any(v < 0 for v in (line.salary, line.allowance, line.extra)):
        raise PayrollError(f"{line.employee.name}: gaji, tunjangan, dan tambahan tidak boleh negatif.")
    if line.pph21_manual is not None and not ZERO <= line.pph21_manual <= 34:
        raise PayrollError(f"{line.employee.name}: tarif PPh 21 manual antara 0 dan 34%.")
    parts = bpjs_parts(line.salary + line.allowance, settings) if line.employee.bpjs else {}
    line.bpjs_company = sum((c for c, _e in parts.values()), ZERO)
    line.bpjs_employee = sum((e for _c, e in parts.values()), ZERO)
    line.bpjs_taxable = sum((parts[k][0] for k in TAXABLE_PREMIUMS if k in parts), ZERO)
    line.pension_employee = sum((parts[k][1] for k in PENSION if k in parts), ZERO)
    line.pph21_base = line.gross + line.bpjs_taxable
    if line.pph21_manual is not None:
        line.pph21_method, line.pph21_rate = "manual", line.pph21_manual
    elif last_period(line):
        line.pph21_method, line.pph21_rate = "annual", ZERO
    else:
        line.pph21_method, line.pph21_rate = "ter", pph21.ter_rate(line.employee.ptkp_status, line.pph21_base)
    if line.pph21_method == "annual":
        line.pph21 = year_end(line)["december"]
    else:
        line.pph21 = withholding.compute("pph21", line.pph21_base, line.pph21_rate)[1] if line.pph21_rate else ZERO
    line.net = line.gross - line.bpjs_employee - line.pph21
    if line.net < 0:
        raise PayrollError(f"{line.employee.name}: potongan melebihi gaji.")
    return line


def slip(line, settings=None):
    """What a payslip prints: earnings, deductions, and the company's own contributions."""
    settings = settings or settings_for(line.run.store)
    parts = bpjs_parts(line.salary + line.allowance, settings) if line.employee.bpjs else {}
    earnings = [("Gaji pokok", line.salary), ("Tunjangan tetap", line.allowance), ("Lembur / bonus", line.extra)]
    deductions = [(f"{name} (karyawan)", e) for name, (_c, e) in parts.items() if e]
    if line.pph21 > 0:
        deductions.append((pph21_label(line), line.pph21))
    if line.pph21 < 0:
        earnings.append(("Pengembalian PPh 21 lebih potong", -line.pph21))
    company = [(name, c) for name, (c, _e) in parts.items() if c]
    return {"earnings": [row for row in earnings if row[1]], "deductions": deductions, "company": company,
        "annual": year_end(line) if line.pph21_method == "annual" else None,
        "gross_paid": line.gross + max(-line.pph21, ZERO),
        "gross": line.gross, "deducted": line.bpjs_employee + max(line.pph21, ZERO), "net": line.net}


# ── The run ─────────────────────────────────────────────────────────────────

def payable_employees(store, period):
    """Active employees on the payroll in ``period``: joined by its last day, not left before its first."""
    return Employee.objects.filter(store=store, is_active=True).filter(
        Q(joined_on__isnull=True) | Q(joined_on__lte=month_end(period))).filter(
        Q(left_on__isnull=True) | Q(left_on__gte=period))


def _line_for(run, employee):
    return PayrollLine(run=run, employee=employee, salary=employee.base_salary, allowance=employee.allowance,
        pph21_manual=employee.pph21_rate)


@transaction.atomic
def create_run(store, period, *, actor=None):
    """Draft the month's payroll from every active employee."""
    from apps.tax.numbering import next_number
    period = period.replace(day=1)
    if PayrollRun.objects.filter(store=store, period=period).exclude(status=PayrollRun.VOID).exists():
        raise PayrollError(f"Penggajian {period:%m/%Y} sudah ada.")
    employees = list(payable_employees(store, period))
    if not employees:
        raise PayrollError("Tidak ada karyawan aktif untuk bulan ini. Tambahkan karyawan dulu.")
    run = PayrollRun.objects.create(store=store, period=period, created_by=actor,
        number=next_number(store, "GJ", month_end(period)))
    settings = settings_for(store)
    for employee in employees:
        compute_line(_line_for(run, employee), settings).save()
    audit_log(store, actor, "payroll.created", run, period=f"{period:%m/%Y}", employees=len(employees))
    return run


def _draft(run):
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != PayrollRun.DRAFT or run.approval_pending:
        raise PayrollError("Hanya penggajian draf yang bisa diubah.")
    return run


@transaction.atomic
def update_lines(run, rows, *, actor=None):
    """``rows`` maps a line id to ``{"salary", "allowance", "extra", "pph21_manual", "remove"}``; a
    ``pph21_manual`` of None means the TER table (or, in December, the year-end recalculation)."""
    run = _draft(run)
    settings = settings_for(run.store)
    for line in run.lines.select_related("employee"):
        row = rows.get(line.pk)
        if row is None:
            continue
        if row.get("remove"):
            line.delete()
            continue
        for field in ("salary", "allowance", "extra"):
            if row.get(field) is not None:
                setattr(line, field, Decimal(row[field]))
        if "pph21_manual" in row:
            line.pph21_manual = None if row["pph21_manual"] is None else Decimal(row["pph21_manual"])
        compute_line(line, settings).save()
    audit_log(run.store, actor, "payroll.updated", run)
    return run


@transaction.atomic
def add_missing_employees(run, *, actor=None):
    """Add active employees who joined after the run was drafted. Returns how many."""
    run = _draft(run)
    settings = settings_for(run.store)
    have = set(run.lines.values_list("employee_id", flat=True))
    added = 0
    for employee in payable_employees(run.store, run.period).exclude(pk__in=have):
        compute_line(_line_for(run, employee), settings).save()
        added += 1
    return added


def _entry_amounts(run):
    lines = list(run.lines.all())
    total = lambda field: sum((getattr(line, field) for line in lines), ZERO)
    return {SALARIES: sum((line.gross for line in lines), ZERO), BPJS_EXPENSE: total("bpjs_company"),
        BPJS_PAYABLE: -(total("bpjs_employee") + total("bpjs_company")), WITHHELD: -total("pph21"),
        WAGES_PAYABLE: -total("net")}


def _post(run, *, strict=False, actor=None):
    return post_entry(run.store, date=run.posted_on or month_end(run.period), strict=strict, actor=actor,
        source=("payroll", run.pk), memo=f"Penggajian {run.period:%m/%Y} · {run.number}",
        amounts=_entry_amounts(run))


@transaction.atomic
def post_run(run, *, date=None, actor=None):
    """Book the run (``default_post_date`` unless ``date``) and record each employee's PPh 21."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != PayrollRun.DRAFT:
        raise PayrollError("Penggajian ini sudah diposting atau dibatalkan.")
    lines = list(run.lines.select_related("employee"))
    if not lines or sum((line.gross for line in lines), ZERO) <= 0:
        raise PayrollError("Penggajian tanpa gaji tidak bisa diposting.")
    run.posted_on = date or default_post_date(run.period)
    run.status, run.approval_pending = PayrollRun.POSTED, False
    run.save(update_fields=["posted_on", "status", "approval_pending"])
    try:
        _post(run, strict=True, actor=actor)
        for line in lines:
            if line.pph21:
                withholding.record(run.store, kind="pph21", date=run.posted_on, base=line.pph21_base,
                    rate=line.pph21_rate, payee_name=line.employee.name, payee_npwp=line.employee.npwp,
                    source_type="payroll_line", source_id=line.pk, actor=actor,
                    amount=line.pph21 if line.pph21_method == "annual" else None)
    except (LedgerError, withholding.WithholdingError) as error:
        raise PayrollError(str(error)) from error
    audit_log(run.store, actor, "payroll.posted", run, gross=str(run.gross), net=str(run.net))
    return run


def _bank_code(store, bank_account):
    if bank_account is not None and bank_account.store_id != store.id:
        raise PayrollError("Rekening tidak ditemukan.")
    return bank_account.account.code if bank_account else CASH


@transaction.atomic
def pay_run(run, *, date, bank_account=None, actor=None):
    """Transfer the take-home pay: Utang gaji falls, the bank falls."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != PayrollRun.POSTED:
        raise PayrollError("Hanya penggajian yang sudah diposting dan belum dibayar yang bisa dibayar.")
    if date < run.posted_on:
        raise PayrollError("Tanggal bayar tidak boleh sebelum tanggal posting.")
    net = run.net
    try:
        post_entry(run.store, date=date, strict=True, actor=actor, source=("payroll_pay", run.pk),
            memo=f"Bayar gaji {run.period:%m/%Y} · {run.number}",
            amounts={WAGES_PAYABLE: net, _bank_code(run.store, bank_account): -net})
    except LedgerError as error:
        raise PayrollError(str(error)) from error
    run.status, run.paid_on, run.paid_from = PayrollRun.PAID, date, bank_account
    run.save(update_fields=["status", "paid_on", "paid_from"])
    audit_log(run.store, actor, "payroll.paid", run, net=str(net))
    return run


@transaction.atomic
def pay_bpjs(run, *, date, bank_account=None, actor=None):
    """Pay the month's BPJS (both shares) to BPJS: Utang BPJS falls, the bank falls."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status not in (PayrollRun.POSTED, PayrollRun.PAID) or run.bpjs_paid_on:
        raise PayrollError("BPJS penggajian ini sudah disetor atau penggajian belum diposting.")
    total = run.bpjs_total
    if total <= 0:
        raise PayrollError("Tidak ada BPJS untuk disetor.")
    try:
        post_entry(run.store, date=date, strict=True, actor=actor, source=("payroll_bpjs", run.pk),
            memo=f"Setor BPJS {run.period:%m/%Y} · {run.number}",
            amounts={BPJS_PAYABLE: total, _bank_code(run.store, bank_account): -total})
    except LedgerError as error:
        raise PayrollError(str(error)) from error
    run.bpjs_paid_on = date
    run.save(update_fields=["bpjs_paid_on"])
    audit_log(run.store, actor, "payroll.bpjs_paid", run, amount=str(total))
    return run


@transaction.atomic
def void_run(run, *, actor=None):
    """A draft is dropped; a posted run is reversed while nothing was paid from it yet."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status == PayrollRun.VOID:
        return run
    if run.status == PayrollRun.PAID or run.bpjs_paid_on:
        raise PayrollError("Gaji atau BPJS sudah dibayar: koreksi lewat jurnal, bukan pembatalan.")
    held = Withholding.objects.filter(store=run.store, source_type="payroll_line",
        source_id__in=[str(pk) for pk in run.lines.values_list("pk", flat=True)])
    if held.filter(deposit__isnull=False).exists():
        raise PayrollError("PPh 21 penggajian ini sudah disetor: batalkan tidak bisa.")
    if run.status == PayrollRun.POSTED:
        try:
            reverse_source(run.store, "payroll", run.pk, strict=True, actor=actor,
                memo=f"Pembatalan penggajian {run.period:%m/%Y} · {run.number}")
        except LedgerError as error:
            raise PayrollError(str(error)) from error
        held.delete()  # undeposited bukti potong of a payroll that no longer stands
    run.status, run.approval_pending = PayrollRun.VOID, False
    run.save(update_fields=["status", "approval_pending"])
    audit_log(run.store, actor, "payroll.voided", run)
    return run


@register_backlog
def post_unposted_payroll():
    posted = 0
    for run in unposted(PayrollRun.objects.filter(status__in=[PayrollRun.POSTED, PayrollRun.PAID]), "payroll"):
        posted += bool(_post(run))
    return posted


def owed(store):
    """``(take-home pay not yet paid, BPJS not yet paid)`` over the live runs."""
    runs = PayrollRun.objects.filter(store=store).exclude(status__in=[PayrollRun.DRAFT, PayrollRun.VOID]
        ).prefetch_related("lines")
    wages = sum((run.net for run in runs if run.status == PayrollRun.POSTED), ZERO)
    bpjs = sum((run.bpjs_total for run in runs if not run.bpjs_paid_on), ZERO)
    return wages, bpjs


def employee_from_form(store, data, employee=None):
    """Create or update an employee from a form's fields. Raises PayrollError."""
    name = (data.get("name") or "").strip()
    if not name:
        raise PayrollError("Isi nama karyawan.")
    employee = employee or Employee(store=store)
    employee.name = name[:120]
    employee.nik = "".join(ch for ch in data.get("nik", "") if ch.isdigit())[:16]
    employee.npwp = "".join(ch for ch in data.get("npwp", "") if ch.isdigit())[:20]
    if data.get("ptkp_status") not in dict(Employee._meta.get_field("ptkp_status").choices):
        raise PayrollError("Pilih status PTKP.")
    employee.ptkp_status = data["ptkp_status"]
    employee.position = (data.get("position") or "")[:80]
    try:
        employee.base_salary = _money(data.get("base_salary"))
        employee.allowance = _money(data.get("allowance"))
        raw = str(data.get("pph21_rate") or "").strip().replace(",", ".")
        employee.pph21_rate = Decimal(raw) if raw else None  # empty: the TER table
    except ArithmeticError:
        raise PayrollError("Angka gaji atau tarif tidak terbaca.") from None
    if employee.base_salary < 0 or employee.allowance < 0 or (employee.pph21_rate is not None
            and not ZERO <= employee.pph21_rate <= 34):
        raise PayrollError("Gaji tidak boleh negatif dan tarif TER antara 0 dan 34%.")
    employee.bpjs = bool(data.get("bpjs"))
    employee.bank_name = (data.get("bank_name") or "")[:60]
    employee.bank_account_number = (data.get("bank_account_number") or "")[:40]
    try:
        employee.joined_on, employee.left_on = (_date(data.get("joined_on")), _date(data.get("left_on")))
    except ValueError:
        raise PayrollError("Tanggal mulai atau berhenti bekerja tidak terbaca.") from None
    if employee.joined_on and employee.left_on and employee.left_on < employee.joined_on:
        raise PayrollError("Tanggal berhenti bekerja tidak boleh sebelum tanggal mulai.")
    employee.is_active = data.get("is_active", "1") not in ("", "0", None)
    employee.save()
    return employee


def _date(text):
    text = (text or "").strip()
    return datetime.date.fromisoformat(text) if text else None


def _money(text):
    return Decimal(str(text or "0").replace(".", "").replace(",", "") or "0")
