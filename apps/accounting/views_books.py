"""Back-office pages for kas & bank, bank reconciliation, the journal, account ledgers, fixed
assets, and the profit-and-loss and cash-flow statements."""
import csv
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from . import assets as asset_service
from . import bank as bank_service
from . import reports
from .ledger import (
    ACCUMULATED_DEPRECIATION,
    EQUITY,
    LOANS,
    PAYABLE,
    LedgerError,
    chart_of_accounts,
    entries_for,
    is_cash_code,
    locked_through,
    reverse_entry,
)
from .models import (
    ASSET_GROUPS,
    Account,
    BankAccount,
    BankStatementLine,
    CashTransaction,
    FixedAsset,
    JournalEntry,
    JournalLine,
)
from .reports import SOURCE_LABELS, source_link

VIEW_ROLES = {"admin", "manager", "finance", "viewer"}
EDIT_ROLES = {"admin", "finance"}
LOGIN = "/merchant/login/"


def _store(request, roles=VIEW_ROLES):
    store = _staff_store(request)
    _require_roles(request, store, roles)
    return store


def _can_edit(request, store):
    return store.owner_id == request.user.id or store.staff_members.filter(
        user=request.user, role__in=EDIT_ROLES).exists()


def _date(value, default=None):
    try:
        return datetime.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _amount(value):
    """A rupiah amount typed as 1.250.000 or 1250000 or 1250000,50."""
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


def _posting_accounts(store, *, exclude_cash=True):
    accounts = sorted(chart_of_accounts(store).values(), key=lambda a: a.code)
    extra = Account.objects.filter(store=store).exclude(code__in=[a.code for a in accounts])
    accounts += list(extra)
    accounts.sort(key=lambda a: a.code)
    if exclude_cash:
        accounts = [a for a in accounts if not is_cash_code(a.code)]
    return accounts


def _grouped(accounts):
    labels = dict(Account.TYPES)
    groups = {}
    for account in accounts:
        groups.setdefault(labels.get(account.type, account.type), []).append(account)
    return list(groups.items())


# ---------------------------------------------------------------------------
# Kas & bank
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_bank_accounts(request):
    store = _store(request)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        try:
            account = bank_service.add_bank_account(store, name=request.POST.get("name", ""),
                kind=request.POST.get("kind", "bank") if request.POST.get("kind") in
                    dict(BankAccount.KINDS) else "bank",
                bank_name=request.POST.get("bank_name", ""),
                account_number=request.POST.get("account_number", ""))
        except bank_service.BankError as error:
            messages.error(request, str(error))
        else:
            _audit(request, store, "bank_account.created", account, name=account.name)
            messages.success(request, f"Rekening {account.name} ditambahkan "
                f"(akun {account.account.code}).")
            return redirect("merchant-bank-account", account.pk)
        return redirect("merchant-bank-accounts")
    accounts = bank_service.bank_accounts(store)
    book = bank_service.balances(store, accounts)
    for account in accounts:
        account.book_balance = book[account.id]
        account.summary = bank_service.reconciliation_summary(account)
    recent = (CashTransaction.objects.filter(store=store)
        .select_related("bank_account", "to_bank_account")[:15])
    return render(request, "merchant/bank_accounts.html", _merchant_context(request, store,
        accounts=accounts, total=sum(book.values(), Decimal(0)), recent=recent,
        kinds=BankAccount.KINDS, can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_bank_account(request, account_id):
    """Rekonsiliasi: the bank's statement on the left, the books on the right."""
    store = _store(request)
    bank = get_object_or_404(BankAccount.objects.select_related("account"), pk=account_id,
        store=store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        return _reconcile_action(request, store, bank)
    tab = request.GET.get("tab", "match")
    lines = bank.statement_lines.select_related("matched_line__entry")
    if tab == "match":
        shown = list(lines.filter(status=BankStatementLine.UNMATCHED)[:100])
        for line in shown:
            line.suggestions = bank_service.suggestions(line)
    else:
        shown = list(lines.order_by("-date", "-id")[:300])
    open_lines = list(bank_service.open_ledger_lines(bank)[:300])
    summary = bank_service.reconciliation_summary(bank)
    return render(request, "merchant/bank_account.html", _merchant_context(request, store,
        bank=bank, tab=tab, lines=shown, open_lines=open_lines, summary=summary,
        accounts=_grouped(_posting_accounts(store)), can_edit=_can_edit(request, store),
        today=timezone.localdate().isoformat()))


def _reconcile_action(request, store, bank):
    action = request.POST.get("action")
    back = redirect(f"{request.path}?tab={request.POST.get('tab', 'match')}")
    if action == "import":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Pilih file CSV mutasi rekening.")
            return back
        if upload.size > 5 * 1024 * 1024:
            messages.error(request, "File terlalu besar (maksimal 5 MB).")
            return back
        try:
            rows, skipped = bank_service.parse_statement(upload.read())
        except bank_service.BankError as error:
            messages.error(request, str(error))
            return back
        added, duplicates = bank_service.import_statement(bank, rows)
        _audit(request, store, "bank_statement.imported", bank, added=added,
            duplicates=duplicates)
        note = f"{added} mutasi baru diimpor"
        if duplicates:
            note += f", {duplicates} sudah ada sebelumnya"
        if skipped:
            note += f", {skipped} baris dilewati (saldo/pending)"
        messages.success(request, note + ".")
        return back
    statement = get_object_or_404(BankStatementLine, pk=request.POST.get("line"),
        bank_account=bank)
    try:
        if action == "match":
            journal_line = get_object_or_404(JournalLine, pk=request.POST.get("journal_line"),
                account=bank.account)
            bank_service.match_line(statement, journal_line, actor=request.user)
            messages.success(request, "Mutasi dicocokkan.")
        elif action == "create":
            bank_service.create_and_match(statement, account_code=request.POST.get("account", ""),
                description=request.POST.get("description", "").strip(),
                contact=request.POST.get("contact", ""), actor=request.user)
            messages.success(request, "Transaksi dicatat dan dicocokkan.")
        elif action == "unmatch":
            bank_service.unmatch_line(statement)
            messages.success(request, "Pencocokan dibatalkan.")
        elif action == "ignore":
            bank_service.ignore_line(statement)
            messages.success(request, "Mutasi diabaikan.")
        elif action == "restore":
            bank_service.unmatch_line(statement)
        else:
            raise Http404
    except (bank_service.BankError, LedgerError) as error:
        messages.error(request, str(error))
    return back


@login_required(login_url=LOGIN)
def merchant_cash_new(request):
    """Terima uang / kirim uang / transfer antar rekening."""
    store = _store(request, EDIT_ROLES)
    banks = bank_service.bank_accounts(store)
    kind = request.POST.get("kind") or request.GET.get("kind") or CashTransaction.SPEND
    if kind not in dict(CashTransaction.KINDS):
        kind = CashTransaction.SPEND
    if request.method == "POST":
        try:
            bank = next(b for b in banks if str(b.pk) == request.POST.get("bank_account"))
            target = next((b for b in banks if str(b.pk) == request.POST.get("to_bank_account")),
                None)
            lines = [(code, text, _amount(value)) for code, text, value in zip(
                request.POST.getlist("account[]"), request.POST.getlist("line_description[]"),
                request.POST.getlist("amount[]"), strict=False)]
            txn = bank_service.record_cash_transaction(store, kind=kind, bank_account=bank,
                date=_date(request.POST.get("date"), timezone.localdate()), lines=lines,
                contact=request.POST.get("contact", ""), reference=request.POST.get("reference", ""),
                description=request.POST.get("description", ""), tax=_amount(request.POST.get("tax")),
                to_bank_account=target, amount=_amount(request.POST.get("amount")),
                actor=request.user)
        except StopIteration:
            messages.error(request, "Pilih rekening.")
        except (bank_service.BankError, LedgerError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Jumlah tidak valid.")
        else:
            _audit(request, store, "cash.created", txn, kind=kind, total=str(txn.total))
            messages.success(request, f"{txn.get_kind_display()} Rp{txn.total:,.0f} dicatat."
                .replace(",", "."))
            if request.POST.get("next") == "new":
                return redirect(f"{request.path}?kind={kind}&bank={txn.bank_account_id}")
            return redirect("merchant-cash-detail", txn.pk)
    return render(request, "merchant/cash_new.html", _merchant_context(request, store,
        kind=kind, kinds=CashTransaction.KINDS, banks=banks,
        selected_bank=request.GET.get("bank") or request.POST.get("bank_account") or (
            banks[0].pk if banks else ""),
        accounts=_grouped(_posting_accounts(store)), today=timezone.localdate().isoformat(),
        locked_through=locked_through(store), post=request.POST,
        suggested={"receive": EQUITY, "spend": "6900"}.get(kind, "")))


@login_required(login_url=LOGIN)
def merchant_cash_detail(request, txn_id):
    store = _store(request)
    txn = get_object_or_404(CashTransaction.objects.select_related(
        "bank_account__account", "to_bank_account", "created_by"), pk=txn_id, store=store)
    if request.method == "POST" and request.POST.get("action") == "void":
        _require_roles(request, store, EDIT_ROLES)
        try:
            bank_service.void_cash_transaction(txn, actor=request.user)
        except (bank_service.BankError, LedgerError) as error:
            messages.error(request, str(error))
        else:
            _audit(request, store, "cash.voided", txn)
            messages.success(request, "Transaksi dibatalkan dan jurnalnya dibalik.")
        return redirect("merchant-cash-detail", txn.pk)
    journal = entries_for(store, [("cash", txn.pk)])
    matched = BankStatementLine.objects.filter(matched_line__entry__in=journal).first()
    return render(request, "merchant/cash_detail.html", _merchant_context(request, store,
        txn=txn, items=txn.lines.select_related("account"), journal=journal, matched=matched,
        can_edit=_can_edit(request, store)))


# ---------------------------------------------------------------------------
# Jurnal & buku besar
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_journal_list(request):
    store = _store(request)
    entries = JournalEntry.objects.filter(store=store).annotate(
        amount=Sum("lines__debit")).select_related("created_by")
    start, end = _date(request.GET.get("start")), _date(request.GET.get("end"))
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("type", "")
    if start:
        entries = entries.filter(date__gte=start)
    if end:
        entries = entries.filter(date__lte=end)
    if query:
        entries = entries.filter(Q(memo__icontains=query) | Q(source_id__icontains=query))
    if kind == "manual":
        entries = entries.filter(source_type="journal")
    elif kind:
        entries = entries.filter(source_type=kind)
    entries = entries.order_by("-date", "-id")
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="jurnal-{store.slug}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Tanggal", "No. jurnal", "Sumber", "Keterangan", "Kode akun",
            "Nama akun", "Debit", "Kredit"])
        for line in JournalLine.objects.filter(entry__in=entries).select_related(
                "entry", "account").order_by("entry__date", "entry_id", "-debit").iterator():
            writer.writerow([line.entry.date.isoformat(), f"JU-{line.entry_id}",
                SOURCE_LABELS.get(line.entry.source_type, line.entry.source_type),
                line.entry.memo, line.account.code, line.account.name, line.debit, line.credit])
        return response
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    types = (JournalEntry.objects.filter(store=store).values_list("source_type", flat=True)
        .distinct().order_by("source_type"))
    filters = request.GET.copy()
    filters.pop("page", None)
    for entry in page:
        entry.source_label = SOURCE_LABELS.get(entry.source_type, entry.source_type)
    return render(request, "merchant/journal_list.html", _merchant_context(request, store,
        page=page, start=start, end=end, query=query, kind=kind,
        types=[(t, SOURCE_LABELS.get(t, t)) for t in types if t],
        filter_query=filters.urlencode(), locked_through=locked_through(store)))


@login_required(login_url=LOGIN)
def merchant_journal_detail(request, entry_id):
    store = _store(request)
    entry = get_object_or_404(JournalEntry.objects.select_related("created_by", "reverses"),
        pk=entry_id, store=store)
    if request.method == "POST" and request.POST.get("action") == "reverse":
        _require_roles(request, store, EDIT_ROLES)
        if entry.source_type != "journal" or entry.superseded:
            messages.error(request, "Hanya jurnal umum yang belum dibatalkan yang bisa dibalik "
                "di sini. Batalkan dokumen sumbernya untuk jurnal otomatis.")
        else:
            try:
                reversal = reverse_entry(entry, date=_date(request.POST.get("date"),
                    timezone.localdate()), strict=True, actor=request.user)
            except LedgerError as error:
                messages.error(request, str(error))
            else:
                _audit(request, store, "journal.reversed", entry, reversal=reversal.pk)
                messages.success(request, "Jurnal dibalik.")
                return redirect("merchant-journal-detail", reversal.pk)
        return redirect("merchant-journal-detail", entry.pk)
    rows = sorted(entry.lines.select_related("account"), key=lambda l: (l.credit > 0, l.account.code))
    label, url = source_link(entry)
    reversal = JournalEntry.objects.filter(reverses=entry).first()
    total = sum((row.debit for row in rows), Decimal(0))
    return render(request, "merchant/journal_detail.html", _merchant_context(request, store,
        entry=entry, rows=rows, total=total, source_label=label, source_url=url,
        reversal=reversal, can_reverse=entry.source_type == "journal" and not entry.superseded
            and _can_edit(request, store), today=timezone.localdate().isoformat()))


@login_required(login_url=LOGIN)
def merchant_account_ledger(request, code):
    store = _store(request)
    chart_of_accounts(store)
    account = get_object_or_404(Account, store=store, code=code)
    today = timezone.localdate()
    start = _date(request.GET.get("start"), today.replace(month=1, day=1))
    end = _date(request.GET.get("end"), today)
    ledger = reports.account_ledger(account, start=start, end=end)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="buku-besar-{code}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Tanggal", "No. jurnal", "Keterangan", "Debit", "Kredit", "Saldo"])
        writer.writerow([start.isoformat(), "", "Saldo awal", "", "", ledger["opening"]])
        for line in ledger["lines"]:
            writer.writerow([line.entry.date.isoformat(), f"JU-{line.entry_id}", line.entry.memo,
                line.debit, line.credit, line.running])
        return response
    bank = BankAccount.objects.filter(account=account).first()
    accounts = Account.objects.filter(store=store).order_by("code")
    return render(request, "merchant/account_ledger.html", _merchant_context(request, store,
        account=account, ledger=ledger, start=start, end=end, bank=bank, accounts=accounts,
        type_label=dict(Account.TYPES).get(account.type, account.type),
        debit_normal=account.type in {"asset", "expense"}))


# ---------------------------------------------------------------------------
# Aset tetap
# ---------------------------------------------------------------------------

def _funding_accounts(store):
    banks = bank_service.bank_accounts(store)
    chart = chart_of_accounts(store)
    return [(b.account.code, f"{b.name} ({b.account.code})") for b in banks] + [
        (code, f"{chart[code].name} ({code})") for code in (PAYABLE, LOANS, EQUITY)]


@login_required(login_url=LOGIN)
def merchant_assets(request):
    store = _store(request)
    today = timezone.localdate()
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        action = request.POST.get("action")
        if action == "depreciate":
            through = _date(request.POST.get("through"), today)
            posted = asset_service.depreciate(store, through=through, actor=request.user)
            total = sum((e.lines.filter(account__code=ACCUMULATED_DEPRECIATION).aggregate(
                c=Sum("credit", default=0))["c"] for e in posted), Decimal(0))
            if posted:
                messages.success(request, f"{len(posted)} jurnal penyusutan dicatat, total "
                    f"Rp{total:,.0f}.".replace(",", "."))
            else:
                messages.success(request, "Penyusutan sudah tercatat sampai tanggal itu.")
            return redirect("merchant-assets")
        try:
            group = request.POST.get("group", "x")
            years = asset_service.GROUP_LIFE.get(group, 0)
            months = years * 12 if years else int(request.POST.get("useful_life_months") or 0)
            asset = asset_service.acquire(store, name=request.POST.get("name", ""),
                number=request.POST.get("number", "").strip(), group=group,
                acquired_on=_date(request.POST.get("acquired_on"), today),
                cost=_amount(request.POST.get("cost")),
                residual_value=_amount(request.POST.get("residual_value")),
                useful_life_months=months, paid_from_code=request.POST.get("paid_from", ""),
                notes=request.POST.get("notes", ""), actor=request.user)
        except (asset_service.AssetError, LedgerError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
            return redirect("merchant-assets")
        _audit(request, store, "asset.created", asset, cost=str(asset.cost))
        messages.success(request, f"Aset {asset.name} dicatat.")
        return redirect("merchant-asset-detail", asset.pk)
    items = list(FixedAsset.objects.filter(store=store))
    totals = {"cost": Decimal(0), "accumulated": Decimal(0), "book": Decimal(0)}
    due = 0
    last_month_end = today.replace(day=1) - datetime.timedelta(days=1)
    for asset in items:
        asset.accumulated = asset_service.accumulated(asset)
        asset.book_value = asset.cost - asset.accumulated
        asset.monthly = asset_service.monthly_charge(asset)
        if asset.status == FixedAsset.ACTIVE:
            totals["cost"] += asset.cost
            totals["accumulated"] += asset.accumulated
            totals["book"] += asset.book_value
            planned = sum((row["amount"] for row in asset_service.schedule(asset)
                if row["month"] <= last_month_end), Decimal(0))
            asset_due = planned - asset.accumulated
            asset.due = asset_due if asset_due > 0 else Decimal(0)
            due += asset.due
    return render(request, "merchant/assets.html", _merchant_context(request, store,
        assets=items, totals=totals, due=due, last_month_end=last_month_end,
        groups=ASSET_GROUPS, funding=_funding_accounts(store), today=today.isoformat(),
        can_edit=_can_edit(request, store)))


@login_required(login_url=LOGIN)
def merchant_asset_detail(request, asset_id):
    store = _store(request)
    asset = get_object_or_404(FixedAsset.objects.select_related("paid_from", "disposal_to",
        "created_by"), pk=asset_id, store=store)
    if request.method == "POST" and request.POST.get("action") == "dispose":
        _require_roles(request, store, EDIT_ROLES)
        try:
            asset, gain = asset_service.dispose(asset,
                disposed_on=_date(request.POST.get("disposed_on"), timezone.localdate()),
                proceeds=_amount(request.POST.get("proceeds")),
                received_into_code=request.POST.get("received_into"), actor=request.user)
        except (asset_service.AssetError, LedgerError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}")
        else:
            _audit(request, store, "asset.disposed", asset, gain=str(gain))
            word = "Laba" if gain >= 0 else "Rugi"
            messages.success(request, f"Aset dilepas. {word} pelepasan Rp{abs(gain):,.0f}."
                .replace(",", "."))
        return redirect("merchant-asset-detail", asset.pk)
    plan = asset_service.schedule(asset)
    posted = set(JournalEntry.objects.filter(store=store, source_type="depreciation",
        source_id__startswith=f"{asset.pk}:", superseded=False).values_list("source_id", flat=True))
    for row in plan:
        row["posted"] = f"{asset.pk}:{row['month']:%Y-%m}" in posted
    accumulated = asset_service.accumulated(asset)
    depreciable = asset.cost - asset.residual_value
    journal = entries_for(store, [("asset", asset.pk), ("asset_disposal", asset.pk)])
    return render(request, "merchant/asset_detail.html", _merchant_context(request, store,
        asset=asset, plan=plan, accumulated=accumulated, book_value=asset.cost - accumulated,
        monthly=asset_service.monthly_charge(asset), journal=journal,
        pct=int(accumulated / depreciable * 100) if depreciable else 100,
        months_posted=len(posted), funding=_funding_accounts(store)[:-3],
        today=timezone.localdate().isoformat(), can_edit=_can_edit(request, store)))


# ---------------------------------------------------------------------------
# Laporan: laba rugi & arus kas
# ---------------------------------------------------------------------------

def _period(request):
    today = timezone.localdate()
    preset = request.GET.get("period", "this_month")
    start, end, label = reports.period(preset, today, _date(request.GET.get("start")),
        _date(request.GET.get("end")))
    if request.GET.get("start") and request.GET.get("end"):
        preset = "custom"
    return preset, start, end, label


@login_required(login_url=LOGIN)
def merchant_profit_loss(request):
    store = _store(request)
    preset, start, end, label = _period(request)
    report = reports.profit_and_loss(store, start, end)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="laba-rugi-{start}-{end}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Laporan laba rugi", store.name, f"{start} s.d. {end}"])
        writer.writerow(["Kode", "Akun", "Periode ini", "Periode sebelumnya"])
        for key, title in [("revenue", "Pendapatan"), ("cogs", "Harga pokok penjualan"),
                ("operating", "Beban operasional"), ("other_income", "Pendapatan lain"),
                ("other_expense", "Beban lain")]:
            writer.writerow(["", title])
            for row in report["sections"][key]:
                writer.writerow([row["code"], row["name"], row["amount"], row["previous"]])
        writer.writerow(["", "Laba bersih", report["totals"]["net"]["amount"],
            report["totals"]["net"]["previous"]])
        return response
    trend = reports.monthly_profit(store, 6, today=timezone.localdate())
    peak = max([abs(row["revenue"]) for row in trend] + [abs(row["expense"]) for row in trend]
        + [Decimal(1)])
    for row in trend:
        row["revenue_pct"] = int(abs(row["revenue"]) / peak * 100)
        row["expense_pct"] = int(abs(row["expense"]) / peak * 100)
    return render(request, "merchant/profit_loss.html", _merchant_context(request, store,
        report=report, start=start, end=end, label=label, preset=preset,
        presets=reports.PRESETS, trend=trend))


@login_required(login_url=LOGIN)
def merchant_cash_flow(request):
    store = _store(request)
    preset, start, end, label = _period(request)
    flow = reports.cash_flow(store, start, end)
    return render(request, "merchant/cash_flow.html", _merchant_context(request, store,
        flow=flow, start=start, end=end, label=label, preset=preset, presets=reports.PRESETS))


@require_POST
@login_required(login_url=LOGIN)
def merchant_bank_account_update(request, account_id):
    store = _store(request, EDIT_ROLES)
    bank = get_object_or_404(BankAccount, pk=account_id, store=store)
    bank.name = (request.POST.get("name") or bank.name).strip()[:120]
    bank.bank_name = request.POST.get("bank_name", bank.bank_name).strip()[:60]
    bank.account_number = request.POST.get("account_number", bank.account_number).strip()[:40]
    bank.is_active = request.POST.get("is_active", "1") == "1"
    bank.save()
    if bank.account.name != bank.name and bank.account.code != "1100":
        bank.account.name = bank.name
        bank.account.save(update_fields=["name"])
    messages.success(request, "Rekening diperbarui.")
    return redirect("merchant-bank-account", bank.pk)


# ---------------------------------------------------------------------------
# Nilai persediaan
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_inventory_value(request):
    """Stock valued at cost against Persediaan in the books, and the adjustment between them."""
    from . import inventory
    store = _store(request)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        contra = request.POST.get("contra", "opening")
        if contra not in inventory.CONTRA:
            contra = "opening"
        try:
            entries = inventory.adjust_books(store, contra=contra, actor=request.user,
                date=_date(request.POST.get("date"), timezone.localdate()))
        except LedgerError as error:
            messages.error(request, str(error))
        else:
            if entries:
                _audit(request, store, "inventory.adjusted", entries[0], contra=contra,
                    entries=len(entries))
                messages.success(request, "Persediaan di buku disesuaikan dengan nilai stok.")
            else:
                messages.success(request, "Buku sudah sama dengan nilai stok.")
        return redirect("merchant-inventory-value")
    data = inventory.reconciliation(store)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="nilai-persediaan-{store.slug}.csv"'
        writer = csv.writer(response)
        writer.writerow(["SKU", "Barang", "Di gudang", "Di mitra", "Harga pokok", "Nilai gudang",
            "Nilai di mitra"])
        for row in data["rows"]:
            writer.writerow([row["sku"], row["name"], row["shelf"], row["mitra"], row["cost"],
                row["shelf_value"], row["mitra_value"]])
        return response
    return render(request, "merchant/inventory_value.html", _merchant_context(request, store,
        data=data, rows=data["rows"][:300], today=timezone.localdate().isoformat(),
        can_edit=_can_edit(request, store), contras=[("opening", "Saldo awal (modal pemilik)"),
        ("loss", "Selisih persediaan (masuk ke HPP)"), ("drawings", "Diambil pemilik (prive)")]))


# ---------------------------------------------------------------------------
# Saldo awal
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_opening_balances(request):
    """Start the books: bank balances, receivables, stock, payables and loans on one day."""
    from . import inventory, opening
    store = _store(request)
    banks = bank_service.bank_accounts(store)
    standing = opening.existing(store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        try:
            if request.POST.get("action") == "undo":
                opening.undo_opening(store, actor=request.user)
                _audit(request, store, "opening.undone", store)
                messages.success(request, "Saldo awal dibatalkan. Anda bisa mencatatnya lagi.")
                return redirect("merchant-opening-balances")
            valuation = inventory.stock_valuation(store)
            entry = opening.post_opening(store,
                date=_date(request.POST.get("date"), timezone.localdate()),
                banks={bank: _amount(request.POST.get(f"bank_{bank.pk}")) for bank in banks},
                receivable=_amount(request.POST.get("receivable")),
                payable=_amount(request.POST.get("payable")),
                loans=_amount(request.POST.get("loans")),
                stock=(valuation["shelf_value"], valuation["mitra_value"])
                    if request.POST.get("include_stock") else None, actor=request.user)
        except (LedgerError, InvalidOperation) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Jumlah tidak valid.")
        else:
            _audit(request, store, "opening.posted", entry, date=str(entry.date))
            messages.success(request, "Saldo awal dicatat. Neraca dimulai dari tanggal itu.")
            return redirect("merchant-opening-balances")
        return redirect("merchant-opening-balances")
    valuation = inventory.stock_valuation(store)
    return render(request, "merchant/opening_balances.html", _merchant_context(request, store,
        banks=banks, standing=standing, lines=opening.breakdown(standing) if standing else [],
        valuation=valuation, today=timezone.localdate().isoformat(),
        can_edit=_can_edit(request, store), locked_through=locked_through(store)))


# ---------------------------------------------------------------------------
# Pemeriksaan pembukuan
# ---------------------------------------------------------------------------

@login_required(login_url=LOGIN)
def merchant_books_checks(request):
    """Does everything reconcile? Every tie between the books and the documents behind them."""
    from . import controls
    from .ledger import post_everything
    store = _store(request)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        posted = post_everything()
        messages.success(request, f"{posted} dokumen yang tertinggal dijurnal." if posted
            else "Tidak ada dokumen yang bisa dijurnal sekarang.")
        return redirect("merchant-books-checks")
    results = controls.run_checks(store, timezone.localdate())
    results.sort(key=lambda c: controls.ORDER[c.status])  # stable: the listed order within a status
    return render(request, "merchant/books_checks.html", _merchant_context(request, store,
        results=results, summary=controls.summarize(results), can_edit=_can_edit(request, store)))
