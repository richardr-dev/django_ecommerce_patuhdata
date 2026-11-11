import csv
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.billing.plans import can_reconcile_payouts
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .ledger import account_balances, chart_of_accounts
from .models import Account, Bill, BillItem, BillPayment, Expense, JournalEntry, Payout, Supplier
from .payouts import (
    PAYOUT_COLUMNS,
    PayoutImportError,
    import_payouts,
    outstanding_by_provider,
    parse_payouts_csv,
    payout_providers,
    unsettled_payments,
)

VIEW_ROLES = {"admin", "manager", "finance", "viewer"}
EDIT_ROLES = {"admin", "finance"}

ACCOUNT_TYPES = [
    ("asset", "Aset"),
    ("liability", "Liabilitas"),
    ("equity", "Ekuitas"),
    ("revenue", "Pendapatan"),
    ("expense", "Beban"),
]


@login_required(login_url="/merchant/login/")
def merchant_accounting(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    chart_of_accounts(store)  # ensure defaults exist
    today = timezone.localdate()
    thirty_days_ago = today.replace(day=1) if today.day <= 30 else today
    import datetime
    thirty_days_ago = today - datetime.timedelta(days=30)
    account_counts = {
        row["type"]: row["n"]
        for row in Account.objects.filter(store=store).values("type").annotate(n=Count("id"))
    }
    total_accounts = sum(account_counts.values())
    recent_expenses = Expense.objects.filter(
        store=store, date__gte=thirty_days_ago
    ).aggregate(total=Sum("amount"))["total"] or Decimal(0)
    recent_journal_count = JournalEntry.objects.filter(
        store=store, date__gte=thirty_days_ago
    ).count()
    return render(request, "merchant/accounting.html", _merchant_context(request, store,
        account_counts=account_counts, total_accounts=total_accounts,
        recent_expenses=recent_expenses, recent_journal_count=recent_journal_count))


@login_required(login_url="/merchant/login/")
def merchant_chart_of_accounts(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    chart_of_accounts(store)  # ensure defaults exist

    errors = {}
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        code = request.POST.get("code", "").strip()[:10]
        name = request.POST.get("name", "").strip()[:120]
        acct_type = request.POST.get("type", "")
        valid_types = {t for t, _ in ACCOUNT_TYPES}
        if not code:
            errors["code"] = "Kode akun wajib diisi."
        elif Account.objects.filter(store=store, code=code).exists():
            errors["code"] = f"Kode {code} sudah digunakan."
        if not name:
            errors["name"] = "Nama akun wajib diisi."
        if acct_type not in valid_types:
            errors["type"] = "Pilih tipe akun."
        if not errors:
            Account.objects.create(store=store, code=code, name=name, type=acct_type)
            messages.success(request, f"Akun {code} {name} berhasil ditambahkan.")
            return redirect("merchant-chart-of-accounts")

    all_accounts = Account.objects.filter(store=store).order_by("code")
    grouped = {}
    for label_key, label_name in ACCOUNT_TYPES:
        grouped[label_key] = {
            "label": label_name,
            "accounts": [a for a in all_accounts if a.type == label_key],
        }

    return render(request, "merchant/chart_of_accounts.html", _merchant_context(request, store,
        grouped=grouped, account_types=ACCOUNT_TYPES, errors=errors,
        post_code=request.POST.get("code", ""),
        post_name=request.POST.get("name", ""),
        post_type=request.POST.get("type", "")))


class PayoutUploadForm(forms.Form):
    provider = forms.ChoiceField(label="Dicairkan oleh", choices=payout_providers)
    file = forms.FileField(label="File CSV pencairan",
        help_text="Template Lakuin: satu baris per pesanan, maksimal 2 MB.")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("File terlalu besar (maksimal 2 MB).")
        return upload


@login_required(login_url="/merchant/login/")
def merchant_payouts(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    form, summary = PayoutUploadForm(request.POST or None, request.FILES or None), None
    can_reconcile = can_reconcile_payouts(store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if not can_reconcile:
            form.add_error(None, "Rekonsiliasi pencairan tersedia mulai paket Pro.")
        elif form.is_valid():
            provider = form.cleaned_data["provider"]
            try:
                grouped = parse_payouts_csv(form.cleaned_data["file"])
            except PayoutImportError as error:
                form.add_error("file", str(error))
            else:
                summary = import_payouts(store, provider, grouped, actor=request.user)
                AuditLog.objects.create(store=store, actor=request.user,
                    action="payouts.imported", object_type="Payout", object_id=provider,
                    details={key: summary[key] for key in ("created", "duplicates")}
                        | {"errors": len(summary["errors"])})
    labels = dict(payout_providers())
    outstanding = outstanding_by_provider(store)
    today = timezone.localdate()
    for row in outstanding:
        row["label"] = labels.get(row["provider"], row["provider"])
        row["age_days"] = (today - timezone.localdate(row["oldest"])).days
    oldest = unsettled_payments(store).select_related("order").order_by("updated_at")[:50]
    payouts = Payout.objects.filter(store=store)[:50]
    for payout in payouts:
        payout.label = labels.get(payout.provider, payout.provider)
        payout.fee_rate = payout.fees * 100 / payout.gross if payout.gross else 0
    return render(request, "merchant/payouts.html", _merchant_context(request, store, form=form,
        summary=summary, outstanding=outstanding, oldest=oldest, payouts=payouts, today=today,
        can_reconcile=can_reconcile))


@login_required(login_url="/merchant/login/")
def merchant_payout_detail(request, payout_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    payout = get_object_or_404(Payout, id=payout_id, store=store)
    payout.label = dict(payout_providers()).get(payout.provider, payout.provider)
    lines = payout.lines.select_related("payment__order").order_by("id")
    return render(request, "merchant/payout_detail.html", _merchant_context(request, store,
        payout=payout, lines=lines))


@login_required(login_url="/merchant/login/")
def payout_template(request):
    _require_roles(request, _staff_store(request), VIEW_ROLES)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="template-pencairan.csv"'
    writer = csv.writer(response)
    writer.writerow(PAYOUT_COLUMNS)
    writer.writerow(["PO-20261102", "2026-11-02", "NK1A2B3C4D", "121000", "2420"])
    return response


# ---------------------------------------------------------------------------
# Accounts Payable: Suppliers
# ---------------------------------------------------------------------------

@login_required(login_url="/merchant/login/")
def merchant_suppliers(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        name = request.POST.get("name", "").strip()[:160]
        if name:
            sup, created = Supplier.objects.get_or_create(store=store, name=name, defaults={
                "contact": request.POST.get("contact", "")[:120],
                "phone": request.POST.get("phone", "")[:30],
                "email": request.POST.get("email", "")[:254],
                "address": request.POST.get("address", ""),
                "npwp": request.POST.get("npwp", "")[:20],
                "notes": request.POST.get("notes", ""),
            })
            if created:
                messages.success(request, f"Supplier {name} ditambahkan.")
            else:
                messages.info(request, f"Supplier {name} sudah ada.")
        return redirect("merchant-suppliers")
    from django.db.models import Q
    suppliers = Supplier.objects.filter(store=store).annotate(
        bill_count=Count("bills"),
        total_owed=Sum("bills__total", filter=Q(bills__status__in=["open", "partial", "overdue"])),
    )
    return render(request, "merchant/suppliers.html", _merchant_context(request, store,
        suppliers=suppliers))


@login_required(login_url="/merchant/login/")
def merchant_supplier_detail(request, supplier_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    supplier = get_object_or_404(Supplier, id=supplier_id, store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if request.POST.get("action") == "edit":
            supplier.contact = request.POST.get("contact", supplier.contact)[:120]
            supplier.phone = request.POST.get("phone", supplier.phone)[:30]
            supplier.email = request.POST.get("email", supplier.email)[:254]
            supplier.address = request.POST.get("address", supplier.address)
            supplier.npwp = request.POST.get("npwp", supplier.npwp)[:20]
            supplier.notes = request.POST.get("notes", supplier.notes)
            supplier.save()
            messages.success(request, "Supplier diperbarui.")
        return redirect("merchant-supplier-detail", supplier_id=supplier.id)
    bills = Bill.objects.filter(store=store, supplier=supplier).prefetch_related("payments")
    for b in bills:
        b._amount_paid = b.amount_paid
        b._amount_due = b.amount_due
    total_owed = sum((b._amount_due for b in bills if b.status in ("open", "partial", "overdue")), Decimal("0"))
    return render(request, "merchant/supplier_detail.html", _merchant_context(request, store,
        supplier=supplier, bills=bills, total_owed=total_owed))


# ---------------------------------------------------------------------------
# Accounts Payable: Bills
# ---------------------------------------------------------------------------

@login_required(login_url="/merchant/login/")
def merchant_bills(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    status_filter = request.GET.get("status", "")
    qs = Bill.objects.filter(store=store).select_related("supplier")
    if status_filter:
        qs = qs.filter(status=status_filter)
    bills = list(qs.prefetch_related("payments"))
    for b in bills:
        b._amount_paid = b.amount_paid
        b._amount_due = b.amount_due
    totals = Bill.objects.filter(store=store, status__in=["open", "partial", "overdue"]).aggregate(
        total_owed=Sum("total"), count=Count("id"))
    return render(request, "merchant/bills.html", _merchant_context(request, store,
        bills=bills, status_filter=status_filter, totals=totals,
        status_choices=Bill.STATUS_CHOICES))


@login_required(login_url="/merchant/login/")
def merchant_bill_new(request):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    suppliers = Supplier.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        from decimal import InvalidOperation
        from django.db import transaction as _tx
        import datetime
        try:
            supplier_id = request.POST.get("supplier_id", "")
            supplier_name = request.POST.get("supplier_name", "").strip()[:160]
            bill_date_str = request.POST.get("bill_date", "")
            due_date_str = request.POST.get("due_date", "")
            number = request.POST.get("number", "")[:40]
            notes = request.POST.get("notes", "")[:300]
            bill_date = datetime.date.fromisoformat(bill_date_str) if bill_date_str else datetime.date.today()
            due_date = datetime.date.fromisoformat(due_date_str) if due_date_str else None

            supplier = None
            if supplier_id:
                try:
                    supplier = Supplier.objects.get(id=int(supplier_id), store=store)
                except (Supplier.DoesNotExist, ValueError):
                    pass
            if not supplier and supplier_name:
                supplier, _ = Supplier.objects.get_or_create(store=store, name=supplier_name)
            if not supplier:
                messages.error(request, "Pilih atau masukkan nama supplier.")
                return redirect("merchant-bill-new")

            descs = request.POST.getlist("description[]")
            qtys = request.POST.getlist("qty[]")
            prices = request.POST.getlist("unit_price[]")
            lines, subtotal = [], Decimal("0")
            for desc, qty_str, price_str in zip(descs, qtys, prices):
                desc = desc.strip()
                if not desc:
                    continue
                try:
                    qty = Decimal(qty_str)
                    unit_price = Decimal(price_str.replace(",", ""))
                except (ValueError, InvalidOperation):
                    continue
                lt = qty * unit_price
                subtotal += lt
                lines.append({"description": desc, "qty": qty, "unit_price": unit_price, "line_total": lt})

            if not lines:
                messages.error(request, "Tambahkan minimal satu item.")
                return redirect("merchant-bill-new")

            try:
                tax = Decimal(request.POST.get("tax", "0").replace(",", ""))
            except Exception:
                tax = Decimal("0")
            total = subtotal + tax

            with _tx.atomic():
                bill = Bill.objects.create(
                    store=store, supplier=supplier, number=number,
                    bill_date=bill_date, due_date=due_date,
                    subtotal=subtotal, tax=tax, total=total,
                    notes=notes, created_by=request.user)
                for line in lines:
                    BillItem.objects.create(bill=bill, description=line["description"],
                        quantity=line["qty"], unit_price=line["unit_price"], line_total=line["line_total"])

            messages.success(request, f"Bill {number or str(bill.id)[:8]} berhasil dibuat.")
            return redirect("merchant-bill-detail", bill_id=bill.id)
        except Exception as exc:
            messages.error(request, f"Gagal: {exc}")

    today = timezone.localdate().isoformat()
    return render(request, "merchant/bill_new.html", _merchant_context(request, store,
        suppliers=suppliers, today=today))


@login_required(login_url="/merchant/login/")
def merchant_bill_detail(request, bill_id):
    import uuid as _uuid
    from django.http import Http404
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        bill = get_object_or_404(Bill, id=_uuid.UUID(str(bill_id)), store=store)
    except (ValueError, AttributeError):
        raise Http404

    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action", "")
        if action == "pay":
            from decimal import InvalidOperation
            import datetime
            try:
                amount = Decimal(request.POST.get("amount", "0").replace(",", ""))
                paid_on_str = request.POST.get("paid_on", "")
                paid_on = datetime.date.fromisoformat(paid_on_str) if paid_on_str else datetime.date.today()
                method = request.POST.get("method", "transfer")
                reference = request.POST.get("reference", "")[:120]
                BillPayment.objects.create(bill=bill, paid_on=paid_on, amount=amount,
                    method=method, reference=reference, recorded_by=request.user)
                total_paid = bill.amount_paid
                if total_paid >= bill.total:
                    bill.status = Bill.STATUS_PAID
                elif total_paid > 0:
                    bill.status = Bill.STATUS_PARTIAL
                bill.save(update_fields=["status"])
                messages.success(request, "Pembayaran dicatat.")
            except Exception as exc:
                messages.error(request, f"Gagal: {exc}")
        elif action == "void":
            bill.status = Bill.STATUS_VOID
            bill.save(update_fields=["status"])
            messages.success(request, "Bill dibatalkan.")
        return redirect("merchant-bill-detail", bill_id=bill.id)

    items = bill.items.all()
    payments = bill.payments.all()
    amount_paid = bill.amount_paid
    amount_due = bill.amount_due
    today = timezone.localdate().isoformat()
    return render(request, "merchant/bill_detail.html", _merchant_context(request, store,
        bill=bill, items=items, payments=payments,
        amount_paid=amount_paid, amount_due=amount_due, today=today))


# ---------------------------------------------------------------------------
# Reports: Balance Sheet & Trial Balance
# ---------------------------------------------------------------------------

@login_required(login_url="/merchant/login/")
def merchant_balance_sheet(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    import datetime
    as_of_str = request.GET.get("as_of", "")
    as_of = datetime.date.fromisoformat(as_of_str) if as_of_str else datetime.date.today()
    chart_of_accounts(store)
    accounts = account_balances(store, end=as_of)

    assets      = [a for a in accounts if a.type == "asset" and a.balance]
    liabilities = [a for a in accounts if a.type == "liability" and a.balance]
    equity_accs = [a for a in accounts if a.type == "equity" and a.balance]
    revenue     = [a for a in accounts if a.type == "revenue"]
    expenses    = [a for a in accounts if a.type == "expense"]

    total_assets      = sum((a.balance for a in assets), Decimal("0"))
    total_liabilities = sum((a.balance for a in liabilities), Decimal("0"))
    retained_earnings = (sum((a.balance for a in revenue), Decimal("0"))
                         - sum((a.balance for a in expenses), Decimal("0")))
    total_equity      = sum((a.balance for a in equity_accs), Decimal("0")) + retained_earnings
    total_l_e         = total_liabilities + total_equity

    return render(request, "merchant/balance_sheet.html", _merchant_context(request, store,
        assets=assets, liabilities=liabilities, equity_accs=equity_accs,
        total_assets=total_assets, total_liabilities=total_liabilities,
        retained_earnings=retained_earnings, total_equity=total_equity,
        total_l_e=total_l_e, as_of=as_of,
        balanced=(abs(total_assets - total_l_e) < Decimal("0.02"))))


@login_required(login_url="/merchant/login/")
def merchant_trial_balance(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    chart_of_accounts(store)
    accounts = account_balances(store)
    total_debit  = sum((a.total_debit for a in accounts), Decimal("0"))
    total_credit = sum((a.total_credit for a in accounts), Decimal("0"))
    return render(request, "merchant/trial_balance.html", _merchant_context(request, store,
        accounts=accounts, total_debit=total_debit, total_credit=total_credit,
        balanced=(abs(total_debit - total_credit) < Decimal("0.02"))))
