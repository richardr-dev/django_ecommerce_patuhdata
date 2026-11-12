"""Kas & bank: the store's money accounts, money that moves without an invoice or bill, and
bank reconciliation.

Every bank, petty-cash or e-wallet account has its own ledger account (1100, 1101...). Money in
and out that no sale or purchase explains (owner capital, a loan, bank interest, an admin fee, a
cash purchase) is a ``CashTransaction``. The bank's statement is imported line by line and each
line is matched to the ledger line it stands for; when every line is matched, the books agree
with the bank."""
import csv
import hashlib
import io
import re
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .ledger import (
    CASH,
    INPUT_VAT,
    VAT,
    LedgerError,
    chart_of_accounts,
    is_cash_code,
    money,
    post_entry,
    register_backlog,
    reverse_source,
    unposted,
)
from .models import (
    Account,
    BankAccount,
    BankStatementLine,
    CashTransaction,
    CashTransactionLine,
    JournalLine,
)


class BankError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def bank_accounts(store, *, active_only=True):
    """The store's money accounts, oldest first. The main cash account (1100) always exists."""
    accounts = chart_of_accounts(store)
    main = accounts[CASH]
    if not BankAccount.objects.filter(account=main).exists():
        BankAccount.objects.get_or_create(account=main,
            defaults={"store": store, "name": "Kas & bank utama", "kind": "bank"})
    found = BankAccount.objects.filter(store=store).select_related("account")
    if active_only:
        found = found.filter(is_active=True)
    return list(found)


def _next_cash_code(store):
    used = set(Account.objects.filter(store=store, code__startswith="11").values_list(
        "code", flat=True))
    for number in range(1101, 1200):
        if str(number) not in used:
            return str(number)
    raise BankError("Batas 99 rekening kas & bank tercapai.")


@transaction.atomic
def add_bank_account(store, *, name, kind="bank", bank_name="", account_number=""):
    name = name.strip()
    if not name:
        raise BankError("Nama rekening wajib diisi.")
    bank_accounts(store)  # the main account takes 1100 first
    if BankAccount.objects.filter(store=store, name__iexact=name).exists():
        raise BankError(f"Rekening “{name}” sudah ada.")
    account = Account.objects.create(store=store, code=_next_cash_code(store),
        name=name[:120], type="asset")
    return BankAccount.objects.create(store=store, account=account, name=name[:120], kind=kind,
        bank_name=bank_name[:60], account_number=account_number[:40])


def balances(store, accounts=None, *, end=None):
    """Ledger balance of each money account, keyed by bank account id."""
    accounts = accounts if accounts is not None else bank_accounts(store)
    lines = JournalLine.objects.filter(account__in=[a.account_id for a in accounts])
    if end:
        lines = lines.filter(entry__date__lte=end)
    sums = {row["account"]: row["d"] - row["c"] for row in lines.values("account").annotate(
        d=Sum("debit", default=0), c=Sum("credit", default=0))}
    return {a.id: sums.get(a.account_id, Decimal(0)) for a in accounts}


# ---------------------------------------------------------------------------
# Terima uang, kirim uang, transfer
# ---------------------------------------------------------------------------

def _amounts(txn):
    bank = txn.bank_account.account.code
    if txn.kind == CashTransaction.TRANSFER:
        return {txn.to_bank_account.account.code: txn.total, bank: -txn.total}
    sign = 1 if txn.kind == CashTransaction.RECEIVE else -1
    amounts = {bank: sign * txn.total}
    for line in txn.lines.select_related("account"):
        amounts[line.account.code] = amounts.get(line.account.code, Decimal(0)) - sign * line.amount
    if txn.tax:
        tax_account = VAT if txn.kind == CashTransaction.RECEIVE else INPUT_VAT
        amounts[tax_account] = amounts.get(tax_account, Decimal(0)) - sign * txn.tax
    return amounts


def post_cash_transaction(txn, *, strict=False, actor=None):
    label = {"receive": "Terima uang", "spend": "Kirim uang", "transfer": "Transfer"}[txn.kind]
    memo = " · ".join(part for part in [label, txn.contact, txn.description] if part)
    if txn.kind == CashTransaction.TRANSFER:
        memo = f"Transfer {txn.bank_account.name} → {txn.to_bank_account.name}"
    return post_entry(txn.store, date=txn.date, memo=memo, amounts=_amounts(txn),
        source=("cash", txn.pk), strict=strict, actor=actor)


@transaction.atomic
def record_cash_transaction(store, *, kind, bank_account, date, lines=(), contact="",
        reference="", description="", tax=0, to_bank_account=None, amount=None, actor=None):
    """Record money in or out (``lines`` is ``[(account_code, description, amount)]``, before
    PPN), or a transfer of ``amount`` to ``to_bank_account``. Posted at once."""
    if kind not in dict(CashTransaction.KINDS):
        raise BankError("Jenis transaksi tidak dikenal.")
    if bank_account.store_id != store.id:
        raise BankError("Rekening tidak ditemukan.")
    tax = money(tax or 0)
    accounts = chart_of_accounts(store)
    rows = []
    if kind == CashTransaction.TRANSFER:
        if not to_bank_account or to_bank_account.store_id != store.id:
            raise BankError("Pilih rekening tujuan.")
        if to_bank_account.pk == bank_account.pk:
            raise BankError("Rekening asal dan tujuan harus berbeda.")
        subtotal, tax = money(amount or 0), Decimal(0)
    else:
        for code, text, value in lines:
            value = money(value or 0)
            if not code and not value:
                continue
            if code not in accounts:
                raise BankError("Pilih akun untuk setiap baris.")
            if is_cash_code(code):
                raise BankError("Untuk memindahkan uang antar rekening, gunakan Transfer.")
            if value <= 0:
                raise BankError("Jumlah setiap baris harus lebih dari nol.")
            rows.append((accounts[code], text.strip()[:200], value))
        if not rows:
            raise BankError("Isi minimal satu baris.")
        subtotal = sum((row[2] for row in rows), Decimal(0))
        if tax < 0:
            raise BankError("PPN tidak boleh negatif.")
    if subtotal <= 0:
        raise BankError("Jumlah harus lebih dari nol.")
    txn = CashTransaction.objects.create(store=store, kind=kind, bank_account=bank_account,
        to_bank_account=to_bank_account if kind == CashTransaction.TRANSFER else None, date=date,
        contact=contact.strip()[:160], reference=reference.strip()[:120],
        description=description.strip()[:300], subtotal=subtotal, tax=tax, total=subtotal + tax,
        created_by=actor)
    CashTransactionLine.objects.bulk_create(CashTransactionLine(transaction=txn, account=account,
        description=text, amount=value) for account, text, value in rows)
    post_cash_transaction(txn, strict=True, actor=actor)
    return txn


@transaction.atomic
def void_cash_transaction(txn, *, actor=None):
    txn = CashTransaction.objects.select_for_update().get(pk=txn.pk)
    if txn.voided:
        return txn
    if BankStatementLine.objects.filter(matched_line__entry__source_type="cash",
            matched_line__entry__source_id=str(txn.pk), matched_line__entry__superseded=False).exists():
        raise BankError("Transaksi ini sudah dicocokkan dengan mutasi bank. Batalkan "
            "pencocokannya dulu di Rekonsiliasi bank.")
    reverse_source(txn.store, "cash", txn.pk, strict=True, actor=actor,
        date=timezone.localdate())
    txn.voided = True
    txn.save(update_fields=["voided"])
    return txn


@register_backlog
def post_cash_backlog(limit=200):
    posted = 0
    for txn in unposted(CashTransaction.objects.filter(voided=False).select_related(
            "store", "bank_account__account", "to_bank_account__account")[:limit], "cash"):
        try:
            with transaction.atomic():
                post_cash_transaction(txn)
            posted += 1
        except (IntegrityError, LedgerError):
            continue
    return posted


# ---------------------------------------------------------------------------
# Bank statements (mutasi rekening)
# ---------------------------------------------------------------------------

_HEADERS = {
    "date": ("tanggal", "tgl", "date", "tanggal transaksi", "transaction date", "posting date",
        "tgl. transaksi", "tanggal mutasi"),
    "description": ("keterangan", "deskripsi", "description", "uraian", "remark", "remarks",
        "keterangan transaksi", "narrative", "detail"),
    "reference": ("referensi", "reference", "no. referensi", "ref", "no referensi", "cabang"),
    "debit": ("debit", "debet", "keluar", "uang keluar", "withdrawal", "mutasi debit", "db"),
    "credit": ("kredit", "credit", "masuk", "uang masuk", "deposit", "mutasi kredit", "cr"),
    "amount": ("jumlah", "amount", "nominal", "mutasi", "nilai"),
    "type": ("db/cr", "d/k", "jenis", "tipe", "type", "dk"),
    "balance": ("saldo", "balance", "saldo akhir", "running balance"),
}


def parse_amount(text):
    """Read 1.250.000,00 / 1,250,000.00 / -15000 / (15.000) / 15.000 CR as a Decimal."""
    text = str(text or "").strip().upper()
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")")) or \
        text.endswith((" DB", "DR"))
    text = re.sub(r"[^\d,.]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):  # 1.250.000,50
            text = text.replace(".", "").replace(",", ".")
        else:  # 1,250,000.50
            text = text.replace(",", "")
    elif "," in text:
        whole, _, tail = text.rpartition(",")
        text = f"{whole.replace(',', '')}.{tail}" if len(tail) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1 or (text.count(".") == 1 and len(text.rpartition(".")[2]) == 3):
        text = text.replace(".", "")  # 1.250.000 or 15.000
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(text, *, year=None):
    text = str(text or "").strip().strip("'")
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d %b %Y",
            "%d %B %Y", "%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)  # BCA: 05/11 without a year
    if match:
        today = timezone.localdate()
        found = Date(year or today.year, int(match[2]), int(match[1]))
        return found if year or found <= today else found.replace(year=found.year - 1)
    return None


def _column(headers, field):
    for index, header in enumerate(headers):
        if header in _HEADERS[field]:
            return index
    return None


def parse_statement(content):
    """Rows of a bank CSV (BCA, Mandiri, BRI, BNI, Jago, or any file with a date, a description
    and either an amount or debit/credit columns) as dicts. Rows before the header are skipped."""
    if isinstance(content, bytes):
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                content = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    sample = content[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(content), dialect))
    header_at = None
    for index, row in enumerate(rows[:30]):
        headers = [cell.strip().lower() for cell in row]
        if _column(headers, "date") is not None and _column(headers, "description") is not None and (
                _column(headers, "amount") is not None or _column(headers, "debit") is not None
                or _column(headers, "credit") is not None):
            header_at = index
            break
    if header_at is None:
        raise BankError("Kolom tanggal, keterangan, dan jumlah (atau debit/kredit) tidak "
            "ditemukan. Unduh mutasi dalam format CSV dari internet banking.")
    headers = [cell.strip().lower() for cell in rows[header_at]]
    cols = {field: _column(headers, field) for field in _HEADERS}
    parsed, skipped = [], 0

    def cell(row, field):
        index = cols[field]
        return row[index].strip() if index is not None and index < len(row) else ""

    for row in rows[header_at + 1:]:
        if not any(part.strip() for part in row):
            continue
        when = parse_date(cell(row, "date"))
        if not when:
            skipped += 1  # PEND (pending) rows, saldo awal/akhir footers
            continue
        if cols["debit"] is not None or cols["credit"] is not None:
            out = parse_amount(cell(row, "debit")) or Decimal(0)
            into = parse_amount(cell(row, "credit")) or Decimal(0)
            amount = abs(into) - abs(out)
        else:
            amount = parse_amount(cell(row, "amount"))
            kind = cell(row, "type").upper()
            after = cols["amount"] + 1  # BCA: an unnamed DB/CR column right after the amount
            if not kind and after < len(row) and headers[after:after + 1] != ["saldo"]:
                kind = row[after].strip().upper()
            if amount is not None and kind in {"DB", "D", "DEBIT", "DEBET", "DR"}:
                amount = -abs(amount)
            elif amount is not None and kind in {"CR", "K", "KREDIT", "CREDIT"}:
                amount = abs(amount)
        if not amount:
            skipped += 1
            continue
        parsed.append({"date": when, "description": " ".join(cell(row, "description").split())[:300],
            "reference": cell(row, "reference").strip("'")[:120], "amount": money(amount),
            "balance": parse_amount(cell(row, "balance"))})
    return parsed, skipped


@transaction.atomic
def import_statement(bank_account, rows):
    """Add statement lines, skipping ones already imported. Returns (added, duplicates)."""
    added = duplicates = 0
    seen = {}
    for row in rows:
        base = f"{row['date']}|{row['amount']}|{row['description']}|{row['reference']}"
        seen[base] = seen.get(base, 0) + 1  # two identical transfers on one day both count
        key = hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()
        _, created = BankStatementLine.objects.get_or_create(bank_account=bank_account,
            import_key=key, defaults={"store_id": bank_account.store_id, "date": row["date"],
                "description": row["description"] or "(tanpa keterangan)",
                "reference": row["reference"], "amount": row["amount"],
                "balance": row.get("balance")})
        added += created
        duplicates += not created
    return added, duplicates


def open_ledger_lines(bank_account):
    """Ledger lines on the account no statement line has claimed yet."""
    return (JournalLine.objects.filter(account=bank_account.account, statement_line__isnull=True,
        entry__superseded=False).exclude(entry__source_type="reversal")
        .select_related("entry").order_by("entry__date", "entry_id"))


def suggestions(statement_line, *, days=7, limit=3):
    """Ledger lines that probably are this statement line: same amount, close in date."""
    amount = statement_line.amount
    side = Q(debit=amount) if amount > 0 else Q(credit=-amount)
    near = Q(entry__date__gte=statement_line.date - timedelta(days=days),
        entry__date__lte=statement_line.date + timedelta(days=days))
    found = list(open_ledger_lines(statement_line.bank_account).filter(side, near)
        .select_related("entry__payout")[:20])
    text = f"{statement_line.description} {statement_line.reference}".lower()

    def rank(line):
        payout = line.entry.payout
        quoted = bool(payout and payout.reference and payout.reference.lower() in text)
        return (not quoted, abs((line.entry.date - statement_line.date).days))
    found.sort(key=rank)
    return found[:limit]


@transaction.atomic
def match_line(statement_line, journal_line, *, actor=None):
    statement_line = BankStatementLine.objects.select_for_update().get(pk=statement_line.pk)
    if statement_line.status == BankStatementLine.MATCHED:
        raise BankError("Mutasi ini sudah dicocokkan.")
    if journal_line.account_id != statement_line.bank_account.account_id:
        raise BankError("Baris jurnal bukan dari rekening ini.")
    if journal_line.debit - journal_line.credit != statement_line.amount:
        raise BankError("Jumlahnya berbeda dengan mutasi bank.")
    if BankStatementLine.objects.filter(matched_line=journal_line).exists():
        raise BankError("Baris jurnal itu sudah dicocokkan dengan mutasi lain.")
    statement_line.matched_line = journal_line
    statement_line.status = BankStatementLine.MATCHED
    statement_line.matched_by, statement_line.matched_at = actor, timezone.now()
    statement_line.save(update_fields=["matched_line", "status", "matched_by", "matched_at"])
    return statement_line


@transaction.atomic
def create_and_match(statement_line, *, account_code, description="", contact="", actor=None):
    """Book a statement line nothing in the ledger explains (bank interest, admin fee, a
    transfer from the owner) and match it in one step."""
    if statement_line.status != BankStatementLine.UNMATCHED:
        raise BankError("Mutasi ini sudah diproses.")
    kind = CashTransaction.RECEIVE if statement_line.amount > 0 else CashTransaction.SPEND
    txn = record_cash_transaction(statement_line.store, kind=kind,
        bank_account=statement_line.bank_account, date=statement_line.date,
        lines=[(account_code, description or statement_line.description, abs(statement_line.amount))],
        contact=contact, reference=statement_line.reference,
        description=description or statement_line.description, actor=actor)
    line = JournalLine.objects.get(entry__source_type="cash", entry__source_id=str(txn.pk),
        entry__superseded=False, account=statement_line.bank_account.account)
    return match_line(statement_line, line, actor=actor)


@transaction.atomic
def unmatch_line(statement_line):
    statement_line.matched_line = None
    statement_line.status = BankStatementLine.UNMATCHED
    statement_line.matched_by = statement_line.matched_at = None
    statement_line.save(update_fields=["matched_line", "status", "matched_by", "matched_at"])
    return statement_line


def ignore_line(statement_line):
    statement_line.status = BankStatementLine.IGNORED
    statement_line.save(update_fields=["status"])
    return statement_line


def reconciliation_summary(bank_account):
    """What the bank says, what the books say, and what explains the difference."""
    lines = bank_account.statement_lines.all()
    last = lines.exclude(balance__isnull=True).order_by("-date", "-id").first()
    unmatched = lines.filter(status=BankStatementLine.UNMATCHED)
    book = balances(bank_account.store, [bank_account])[bank_account.id]
    return {
        "statement_balance": last.balance if last else None,
        "statement_date": last.date if last else None,
        "book_balance": book,
        "unmatched_count": unmatched.count(),
        "unmatched_total": unmatched.aggregate(total=Sum("amount", default=0))["total"],
        "matched_count": lines.filter(status=BankStatementLine.MATCHED).count(),
        "open_ledger_count": open_ledger_lines(bank_account).count(),
        "last_import": lines.order_by("-created_at").values_list("created_at", flat=True).first(),
    }
