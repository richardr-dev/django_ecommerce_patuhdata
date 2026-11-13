import csv
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.billing.plans import can_reconcile_payouts
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.audit import log as audit_log
from apps.shops.audit import trail as audit_trail
from apps.shops.models import AuditLog

from . import approvals
from .attachments import for_record as attachment_list
from .ledger import account_balances, chart_of_accounts, locked_through
from .models import (
    BILL_CATEGORIES,
    WITHHOLDING_KINDS,
    Account,
    BankAccount,
    BankStatementLine,
    Bill,
    BillPayment,
    CostCenter,
    Expense,
    JournalEntry,
    LedgerLock,
    Payout,
    Supplier,
)
from .payables import PayableError, create_bill, pay_bill, receive_bill, void_bill
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


def _banks(store):
    from .bank import bank_accounts
    return bank_accounts(store)


def render_partial(request, page, partial, context):
    """The whole page, or only ``partial`` for an htmx request (a filter or search that swaps one
    region). A history restore asks for the whole page, and the answer varies by header."""
    htmx = request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request")
    response = render(request, partial if htmx else page, context)
    response["Vary"] = "HX-Request"
    return response

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
        store=store, date__gte=thirty_days_ago, approval_pending=False
    ).aggregate(total=Sum("amount"))["total"] or Decimal(0)
    recent_journal_count = JournalEntry.objects.filter(
        store=store, date__gte=thirty_days_ago
    ).count()
    from apps.tax.receivables import aging as receivables_aging

    from . import bank as bank_service
    from . import reports
    from .payables import aging as payables_aging
    from .payables import due_soon
    month_start = today.replace(day=1)
    pl = reports.profit_and_loss(store, month_start, today, compare=True)
    cash_total = sum(bank_service.balances(store).values(), Decimal(0))
    _, ar = receivables_aging(store, today)
    _, ap = payables_aging(store, today)
    unmatched = BankStatementLine.objects.filter(store=store, status="unmatched").count()
    return render(request, "merchant/accounting.html", _merchant_context(request, store,
        pl=pl, cash_total=cash_total, ar=ar, ap=ap, bills_soon=due_soon(store, today)[:5],
        unmatched=unmatched, trend=reports.monthly_profit(store, 6, today=today),
        account_counts=account_counts, total_accounts=total_accounts,
        recent_expenses=recent_expenses, recent_journal_count=recent_journal_count,
        locked_through=locked_through(store), can_lock=request.user == store.owner
            or _has_role(request, store, EDIT_ROLES)))


def _has_role(request, store, roles):
    return store.staff_members.filter(user=request.user, role__in=roles).exists()


@login_required(login_url="/merchant/login/")
def merchant_ledger_lock(request):
    """Tutup buku: close the books through a date, or reopen them by sending no date."""
    import datetime
    if request.method != "POST":
        return redirect("merchant-accounting")
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    value = request.POST.get("locked_through", "")
    if not value:
        LedgerLock.objects.filter(store=store).delete()
        action, detail = "ledger.unlocked", {}
        messages.success(request, "Pembukuan dibuka kembali.")
    else:
        try:
            through = datetime.date.fromisoformat(value)
        except ValueError:
            messages.error(request, "Tanggal tidak valid.")
            return redirect("merchant-accounting")
        LedgerLock.objects.update_or_create(store=store,
            defaults={"locked_through": through, "locked_by": request.user})
        action, detail = "ledger.locked", {"through": through.isoformat()}
        messages.success(request, f"Pembukuan sampai {through:%d/%m/%Y} ditutup.")
    AuditLog.objects.create(store=store, actor=request.user, action=action,
        object_type="LedgerLock", object_id=str(store.id), details=detail)
    return redirect("merchant-accounting")


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
    bank_account = forms.ModelChoiceField(BankAccount.objects.none(), required=False,
        empty_label="Kas utama", label="Masuk ke rekening",
        help_text="Rekening yang menerima uangnya, agar cocok dengan mutasi banknya.")

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
    form.fields["bank_account"].queryset = BankAccount.objects.filter(
        pk__in=[a.pk for a in _banks(store)])
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
                summary = import_payouts(store, provider, grouped, actor=request.user,
                    bank_account=form.cleaned_data.get("bank_account"))
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
    query = request.GET.get("q", "").strip()
    suppliers = Supplier.objects.filter(store=store).annotate(
        bill_count=Count("bills"),
        total_owed=Sum("bills__total", filter=Q(bills__status__in=["open", "partial", "overdue"])),
    )
    if query:
        suppliers = suppliers.filter(Q(name__icontains=query) | Q(contact__icontains=query)
            | Q(phone__icontains=query) | Q(npwp__icontains=query))
    context = _merchant_context(request, store, suppliers=suppliers, query=query,
        can_edit=_has_role(request, store, EDIT_ROLES) or store.owner_id == request.user.id)
    return render_partial(request, "merchant/suppliers.html", "merchant/_suppliers_table.html",
        context)


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
    bills = list(Bill.objects.filter(store=store, supplier=supplier).prefetch_related("payments"))
    total_owed = sum((b.amount_due for b in bills if b.status in ("open", "partial", "overdue")), Decimal(0))
    live = [b for b in bills if b.status != Bill.STATUS_VOID]
    overdue = sum((b.amount_due for b in live if b.due_date and b.due_date < timezone.localdate()
        and b.amount_due > 0), Decimal(0))
    return render(request, "merchant/supplier_detail.html", _merchant_context(request, store,
        supplier=supplier, bills=bills, total_owed=total_owed, overdue=overdue,
        total_billed=sum((b.total for b in live), Decimal(0)),
        total_paid=sum((b.amount_paid for b in live), Decimal(0))))


# ---------------------------------------------------------------------------
# Accounts Payable: Bills
# ---------------------------------------------------------------------------

@login_required(login_url="/merchant/login/")
def merchant_bills(request):
    from django.db.models import Q
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    status_filter = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    qs = Bill.objects.filter(store=store).select_related("supplier")
    if status_filter:
        qs = qs.filter(status=status_filter)
    if query:
        qs = qs.filter(Q(supplier__name__icontains=query) | Q(number__icontains=query))
    bills = list(qs.prefetch_related("payments")[:300])
    today = timezone.localdate()
    for bill in bills:
        bill.late = bool(bill.due_date and bill.due_date < today and bill.amount_due > 0
            and bill.status not in ("void", "paid"))
    open_bills = [b for b in Bill.objects.filter(store=store, status__in=["open", "partial", "overdue"])
        .prefetch_related("payments")]
    totals = {"count": len(open_bills),
        "total_owed": sum((b.amount_due for b in open_bills), Decimal(0)),
        "overdue": sum((b.amount_due for b in open_bills if b.due_date and b.due_date < today),
            Decimal(0))}
    totals["overdue_count"] = sum(1 for b in open_bills if b.due_date and b.due_date < today)
    context = _merchant_context(request, store, bills=bills, status_filter=status_filter,
        query=query, totals=totals, status_choices=Bill.STATUS_CHOICES)
    return render_partial(request, "merchant/bills.html", "merchant/_bills_table.html", context)


@login_required(login_url="/merchant/login/")
def merchant_bill_new(request):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    suppliers = Supplier.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        import datetime
        from decimal import InvalidOperation

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
            skus = request.POST.getlist("sku[]")
            skus += [""] * (len(descs) - len(skus))
            lines, subtotal = [], Decimal(0)
            for desc, qty_str, price_str, sku in zip(descs, qtys, prices, skus):
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
                lines.append({"description": desc, "qty": qty, "unit_price": unit_price, "line_total": lt,
                              "sku": sku})

            try:
                tax = Decimal(request.POST.get("tax", "0").replace(",", "") or "0")
            except InvalidOperation:
                tax = Decimal(0)
            bill = create_bill(store, supplier=supplier, lines=lines, bill_date=bill_date,
                due_date=due_date, number=number, tax=tax, notes=notes, actor=request.user,
                category=request.POST.get("category") or "1400",
                cost_center=CostCenter.objects.filter(store=store, is_active=True,
                    pk=request.POST.get("cost_center") or 0).first())

            audit_log(store, request.user, "bill.created", bill, total=bill.total, supplier=supplier.name)
            messages.success(request, f"Bill {number or str(bill.id)[:8]} berhasil dibuat.")
            return redirect("merchant-bill-detail", bill_id=bill.id)
        except Exception as exc:
            messages.error(request, f"Gagal: {exc}")

    today = timezone.localdate().isoformat()
    return render(request, "merchant/bill_new.html", _merchant_context(request, store,
        suppliers=suppliers, today=today, bill_categories=BILL_CATEGORIES,
        cost_centers=CostCenter.objects.filter(store=store, is_active=True)))


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
            import datetime
            from decimal import InvalidOperation
            try:
                amount = Decimal(request.POST.get("amount", "0").replace(",", ""))
                paid_on_str = request.POST.get("paid_on", "")
                paid_on = datetime.date.fromisoformat(paid_on_str) if paid_on_str else datetime.date.today()
                from .bank import bank_accounts
                bank = next((b for b in bank_accounts(store)
                    if str(b.pk) == request.POST.get("bank_account")), None)
                pph_kind = request.POST.get("pph_kind") or ""
                pph_rate = request.POST.get("pph_rate") or None
                if approvals.required(store, "bill_payment", amount, request.user):
                    approvals.submit(store, "bill_payment", amount,
                        f"Bayar tagihan {bill.supplier.name} {bill.number}".strip(),
                        {"bill_id": str(bill.pk), "amount": str(amount), "paid_on": paid_on.isoformat(),
                         "method": request.POST.get("method", "transfer"),
                         "reference": request.POST.get("reference", "")[:120],
                         "pph_kind": pph_kind, "pph_rate": pph_rate,
                         "bank_account_id": bank.pk if bank else None}, request.user, subject=bill)
                    messages.info(request, "Pembayaran ini perlu persetujuan orang lain karena nilainya besar. "
                        "Sudah diajukan di menu Persetujuan.")
                    return redirect("merchant-bill-detail", bill_id=bill.id)
                pay_bill(bill, amount=amount, paid_on=paid_on,
                    method=request.POST.get("method", "transfer"),
                    reference=request.POST.get("reference", "")[:120], actor=request.user,
                    bank_account=bank, withholding=(pph_kind, pph_rate) if pph_kind else None)
                audit_log(store, request.user, "bill.paid", bill, amount=amount)
                messages.success(request, "Pembayaran dicatat.")
            except (PayableError, InvalidOperation, ValueError) as exc:
                messages.error(request, f"Gagal: {exc}")
        elif action == "receive":
            try:
                received = receive_bill(bill, actor=request.user)
                messages.success(request, f"{received} unit barang masuk ke stok." if received
                    else "Tidak ada barang yang perlu diterima.")
            except PayableError as exc:
                messages.error(request, f"Gagal: {exc}")
        elif action == "void":
            try:
                if approvals.required(store, "bill_void", bill.total, request.user):
                    approvals.submit(store, "bill_void", bill.total,
                        f"Batalkan tagihan {bill.supplier.name} {bill.number}".strip(),
                        {"bill_id": str(bill.pk)}, request.user, subject=bill)
                    messages.info(request, "Pembatalan ini perlu persetujuan orang lain. Sudah diajukan.")
                    return redirect("merchant-bill-detail", bill_id=bill.id)
                void_bill(bill, actor=request.user)
                audit_log(store, request.user, "bill.voided", bill, total=bill.total)
                messages.success(request, "Bill dibatalkan.")
            except PayableError as exc:
                messages.error(request, f"Gagal: {exc}")
        return redirect("merchant-bill-detail", bill_id=bill.id)

    from .ledger import entries_for
    items = bill.items.select_related("product", "variant")
    payments = bill.payments.all()
    amount_paid = bill.amount_paid
    amount_due = bill.amount_due
    today = timezone.localdate().isoformat()
    journal = entries_for(store, [("bill", bill.pk)] + [("bill_payment", p.pk) for p in payments])
    goods = [item for item in items if item.product_id]
    to_receive = sum((int(item.quantity) - item.received_quantity for item in goods
        if int(item.quantity) > item.received_quantity), 0)
    return render(request, "merchant/bill_detail.html", _merchant_context(request, store,
        bill=bill, items=items, payments=payments, journal=journal, audit=audit_trail(store, bill),
        attachments=attachment_list(store, "bill", bill.pk), attach_kind="bill", attach_id=bill.pk,
        amount_paid=amount_paid, amount_due=amount_due, today=today,
        paid_pct=min(100, int(amount_paid * 100 / bill.total)) if bill.total else 0,
        has_goods=bool(goods), to_receive=to_receive,
        payment_methods=BillPayment.METHOD_CHOICES, banks=_banks(store), pph_kinds=WITHHOLDING_KINDS))


# ---------------------------------------------------------------------------
# Reports: Balance Sheet & Trial Balance
# ---------------------------------------------------------------------------

@login_required(login_url="/merchant/login/")
def merchant_balance_sheet(request):
    from . import reports
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    import datetime
    try:
        as_of = datetime.date.fromisoformat(request.GET.get("as_of", "")) \
            if request.GET.get("as_of") else timezone.localdate()
    except ValueError:
        as_of = timezone.localdate()
    compare = reports._shift_months(as_of, -1)
    sheet = reports.balance_sheet(store, as_of, compare_to=compare)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="neraca-{as_of}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Neraca", store.name, str(as_of)])
        writer.writerow(["Kode", "Akun", as_of.isoformat(), compare.isoformat()])
        for key, title in [("current_assets", "Aset lancar"), ("fixed_assets", "Aset tetap"),
                ("current_liabilities", "Liabilitas jangka pendek"),
                ("long_liabilities", "Liabilitas jangka panjang"), ("equity", "Ekuitas")]:
            writer.writerow(["", title])
            for row in sheet["sections"][key]:
                writer.writerow([row["code"], row["name"], row["amount"], row["previous"]])
        return response
    return render(request, "merchant/balance_sheet.html", _merchant_context(request, store,
        sheet=sheet, as_of=as_of, compare=compare))


@login_required(login_url="/merchant/login/")
def merchant_payables(request):
    """Umur utang: what is owed to each supplier and how late."""
    import datetime

    from .payables import AGING_BUCKETS, aging, due_soon, open_bills
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    try:
        as_of = datetime.date.fromisoformat(request.GET.get("as_of", "")) \
            if request.GET.get("as_of") else timezone.localdate()
    except ValueError:
        as_of = timezone.localdate()
    rows, totals = aging(store, as_of)
    totals["overdue"] = totals["total"] - totals["current"]
    from .ledger import PAYABLE
    book = {a.code: a.balance for a in account_balances(store, end=as_of)}.get(PAYABLE, 0)
    supplier = None
    lines = []
    if request.GET.get("supplier", "").isdigit():
        supplier = Supplier.objects.filter(store=store, pk=request.GET["supplier"]).first()
    if supplier:
        for bill in open_bills(store, supplier_id=supplier.pk):
            lines.append({"bill": bill, "paid": bill.amount_paid, "due": bill.amount_due,
                "late_days": max(0, (as_of - (bill.due_date or bill.bill_date)).days)})
    return render(request, "merchant/payables.html", _merchant_context(request, store,
        book_balance=book, unitemised=book - totals["total"],
        rows=rows, totals=totals, buckets=AGING_BUCKETS, as_of=as_of, supplier=supplier,
        lines=lines, soon=due_soon(store, as_of)))


@login_required(login_url="/merchant/login/")
def merchant_trial_balance(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    chart_of_accounts(store)
    accounts = account_balances(store)
    total_debit  = sum((a.total_debit for a in accounts), Decimal(0))
    total_credit = sum((a.total_credit for a in accounts), Decimal(0))
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="neraca-saldo-{store.slug}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Kode", "Akun", "Tipe", "Debit", "Kredit", "Saldo"])
        for account in accounts:
            writer.writerow([account.code, account.name, account.get_type_display(),
                account.total_debit, account.total_credit, account.balance])
        writer.writerow(["", "Total", "", total_debit, total_credit, ""])
        return response
    return render(request, "merchant/trial_balance.html", _merchant_context(request, store,
        accounts=accounts, total_debit=total_debit, total_credit=total_credit,
        balanced=(abs(total_debit - total_credit) < Decimal("0.02"))))


@login_required(login_url="/merchant/login/")
def merchant_journal_new(request):
    """Jurnal umum: saldo awal, setoran modal, penyusutan, dan koreksi."""
    import datetime

    from .ledger import LedgerError, post_journal
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    accounts = list(chart_of_accounts(store).values())
    accounts.sort(key=lambda account: account.code)
    if request.method == "POST":
        try:
            date = datetime.date.fromisoformat(request.POST.get("date", ""))
            lines = []
            for code, debit, credit in zip(request.POST.getlist("account[]"),
                    request.POST.getlist("debit[]"), request.POST.getlist("credit[]"), strict=False):
                debit = Decimal((debit or "0").replace(".", "").replace(",", ".") or "0")
                credit = Decimal((credit or "0").replace(".", "").replace(",", ".") or "0")
                if not code and not debit and not credit:
                    continue
                if code not in {account.code for account in accounts}:
                    raise LedgerError("Pilih akun untuk setiap baris.")
                if debit < 0 or credit < 0 or (debit and credit):
                    raise LedgerError("Isi debit atau kredit saja pada satu baris.")
                lines.append((code, debit, credit))
            memo = request.POST.get("memo", "").strip()
            if not memo:
                raise LedgerError("Keterangan jurnal wajib diisi.")
            total_debit = sum((line[1] for line in lines), Decimal(0))
            total_credit = sum((line[2] for line in lines), Decimal(0))
            if total_debit != total_credit:
                raise LedgerError(f"Jurnal tidak seimbang: debit Rp{total_debit:,.0f}, "
                    f"kredit Rp{total_credit:,.0f}.")
            if approvals.required(store, "journal", total_debit, request.user):
                center = CostCenter.objects.filter(store=store, is_active=True,
                    pk=request.POST.get("cost_center") or 0).first()
                approvals.submit(store, "journal", total_debit, f"Jurnal manual: {memo}",
                    {"date": date.isoformat(), "memo": memo, "cost_center_id": center.pk if center else None,
                     "lines": [[code, str(d), str(c)] for code, d, c in lines]}, request.user)
                messages.info(request, "Jurnal ini perlu persetujuan orang lain karena nilainya besar. "
                    "Sudah diajukan di menu Persetujuan.")
                return redirect("merchant-journal-new")
            entry = post_journal(store, date=date, memo=memo, lines=lines, actor=request.user,
                cost_center=CostCenter.objects.filter(store=store, is_active=True,
                    pk=request.POST.get("cost_center") or 0).first())
            AuditLog.objects.create(store=store, actor=request.user, action="journal.created",
                object_type="JournalEntry", object_id=str(entry.id),
                details={"memo": memo, "debit": str(total_debit)})
            messages.success(request, "Jurnal dicatat.")
            return redirect("merchant-finance")
        except (LedgerError, ValueError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}")
    return render(request, "merchant/journal_new.html", _merchant_context(request, store,
        accounts=accounts, today=timezone.localdate().isoformat(),
        opening=request.GET.get("opening") == "1", locked_through=locked_through(store),
        cost_centers=CostCenter.objects.filter(store=store, is_active=True)))
