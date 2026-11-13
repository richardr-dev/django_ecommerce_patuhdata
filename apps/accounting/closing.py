"""Tutup buku tahunan: move a year's profit into Laba ditahan (3300) and lock the year.

Closing posts one entry on 31 December that zeroes every revenue and expense account against
Laba ditahan, then locks the books through that day. The entry is the document
``("year_close", year)``, so it is idempotent like every other poster, and reopening a year
reverses it instead of deleting it. Reports leave closing entries out (``exclude_closing``), so
a closed year still shows its profit; only the trial balance shows 3300 holding it."""
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from .ledger import (
    RETAINED,
    YEAR_CLOSE,
    LedgerError,
    account_balances,
    chart_of_accounts,
    live_entry,
    post_entry,
    reverse_entry,
)
from .models import JournalEntry, LedgerLock

ZERO = Decimal(0)


class ClosingError(ValueError):
    pass


def year_end(year):
    return date(year, 12, 31)


def profit_of(store, year):
    """Revenue minus expense for the year, as the reports see it (closing entries left out)."""
    accounts = account_balances(store, start=date(year, 1, 1), end=year_end(year),
        exclude_closing=True)
    return (sum((a.balance for a in accounts if a.type == "revenue"), ZERO)
        - sum((a.balance for a in accounts if a.type == "expense"), ZERO))


def closed_years(store):
    """Years with a standing closing entry, oldest first."""
    entries = JournalEntry.objects.filter(store=store, source_type=YEAR_CLOSE, superseded=False)
    return sorted(int(entry.source_id) for entry in entries)


def years(store, today):
    """Rows for the closing page: every year with postings up to this one, with its profit and
    whether it is closed. Only the oldest open year can be closed, so years close in order."""
    first = JournalEntry.objects.filter(store=store).order_by("date").values_list("date", flat=True).first()
    done = set(closed_years(store))
    rows, blocked = [], False
    for year in range(first.year if first else today.year, today.year + 1):
        closed = year in done
        rows.append({"year": year, "profit": profit_of(store, year), "closed": closed,
            "can_close": not closed and not blocked and year < today.year,
            "future": year >= today.year})
        blocked = blocked or not closed
    for row in rows:
        row["can_reopen"] = row["closed"] and row["year"] == max(done, default=None)
    return list(reversed(rows))


@transaction.atomic
def close_year(store, year, *, today, actor=None):
    if year >= today.year:
        raise ClosingError("Tahun berjalan belum bisa ditutup. Tutup setelah tahun berakhir.")
    if live_entry(store, YEAR_CLOSE, year):
        raise ClosingError(f"Tahun {year} sudah ditutup.")
    first = JournalEntry.objects.filter(store=store).order_by("date").values_list("date", flat=True).first()
    done = set(closed_years(store))
    for before in range(first.year if first else year, year):
        if before not in done:
            raise ClosingError(f"Tutup tahun {before} terlebih dahulu.")
    lock = LedgerLock.objects.filter(store=store).first()
    if lock and lock.locked_through >= year_end(year):
        raise ClosingError("Pembukuan sudah dikunci sampai akhir tahun ini. "
            "Buka kunci ke tanggal sebelumnya, lalu tutup tahun.")
    chart_of_accounts(store)
    accounts = account_balances(store, start=date(year, 1, 1), end=year_end(year),
        exclude_closing=True)
    amounts = {}
    for account in accounts:
        if account.type == "revenue" and account.balance:
            amounts[account.code] = account.balance          # debit a credit balance
        elif account.type == "expense" and account.balance:
            amounts[account.code] = -account.balance         # credit a debit balance
    profit = sum((a.balance for a in accounts if a.type == "revenue"), ZERO) \
        - sum((a.balance for a in accounts if a.type == "expense"), ZERO)
    amounts[RETAINED] = -profit
    try:
        entry = post_entry(store, date=year_end(year), strict=True, actor=actor,
            memo=f"Penutupan tahun {year}: laba ke Laba ditahan", amounts=amounts,
            source=(YEAR_CLOSE, year))
    except LedgerError as error:
        raise ClosingError(str(error)) from error
    LedgerLock.objects.update_or_create(store=store,
        defaults={"locked_through": year_end(year), "locked_by": actor})
    return entry, profit


@transaction.atomic
def reopen_year(store, year, *, actor=None):
    """Undo the latest closing: reverse the entry, and keep the year before locked if it is closed. Only the newest closed year can be reopened."""
    closed = closed_years(store)
    if year not in closed:
        raise ClosingError(f"Tahun {year} belum ditutup.")
    if year != max(closed):
        raise ClosingError(f"Buka kembali tahun {max(closed)} terlebih dahulu.")
    entry = live_entry(store, YEAR_CLOSE, year)
    LedgerLock.objects.filter(store=store).delete()
    try:
        reverse_entry(entry, strict=True, actor=actor, memo=f"Pembukaan kembali tahun {year}")
    except LedgerError as error:
        raise ClosingError(str(error)) from error
    if year - 1 in closed:  # the earlier closed year stays locked
        LedgerLock.objects.create(store=store, locked_through=year_end(year - 1), locked_by=actor)
    return entry


def month_checklist(store, year, month, today):
    """What should be true before a month is locked. Each step is ``{"title", "done", "detail",
    "url", "action"}``; ``done`` steps need nothing. Nothing here blocks locking: the list says
    what is still open so a person decides."""
    from django.urls import reverse

    from apps.tax.models import Invoice, Quote

    from . import controls
    from .models import BankStatementLine, Bill, Withholding
    from .todo import depreciation_due

    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1)).replace(day=1)
    results = {r.key: r for r in controls.run_checks(store, today)}
    steps = []

    def step(title, done, detail, url="", action=""):
        steps.append({"title": title, "done": done, "detail": detail, "url": url, "action": action})

    failing = [r for r in results.values() if r.status in (controls.FAIL, controls.ERROR)]
    step("Pemeriksaan pembukuan tidak ada yang gagal", not failing,
        "Semua angka saling cocok." if not failing else f"{len(failing)} pemeriksaan gagal: "
        + ", ".join(r.title for r in failing[:3]), reverse("merchant-books-checks"), "Buka pemeriksaan")
    unposted = results.get("unposted")
    step("Semua dokumen sudah masuk buku besar", not unposted or unposted.status == controls.OK,
        "Tidak ada dokumen tertunda." if not unposted or unposted.status == controls.OK else unposted.summary,
        reverse("merchant-books-checks"), "Periksa")
    lines = BankStatementLine.objects.filter(store=store, status="unmatched", date__lt=last).count()
    step("Mutasi bank sudah dicocokkan", not lines, "Semua mutasi sudah dicocokkan." if not lines
        else f"{lines} mutasi belum dicocokkan.", reverse("merchant-bank-accounts"), "Cocokkan")
    owed = depreciation_due(store, last)
    step("Penyusutan aset bulan ini sudah dicatat", not owed, "Penyusutan sudah dicatat." if not owed
        else f"Penyusutan Rp{owed:,.0f} belum dicatat.".replace(",", "."), reverse("merchant-assets"), "Catat")
    drafts = (Invoice.objects.filter(store=store, status=Invoice.STATUS_DRAFT, issued_on__lt=last).count()
        + Bill.objects.filter(store=store, status=Bill.STATUS_DRAFT, bill_date__lt=last).count()
        + Quote.objects.filter(store=store, status=Quote.STATUS_DRAFT, issued_on__lt=last).count())
    step("Tidak ada draft invoice, tagihan, atau penawaran", not drafts,
        "Tidak ada draft." if not drafts else f"{drafts} draft belum diterbitkan atau dihapus.",
        reverse("merchant-sales-invoices"), "Lihat draft")
    pph = Withholding.objects.filter(store=store, deposit__isnull=True, date__lt=last).count()
    step("PPh yang dipotong sudah disetor", not pph, "Semua PPh sudah disetor." if not pph
        else f"{pph} pemotongan belum disetor.", reverse("merchant-withholdings"), "Setor")
    minus = results.get("negative_stock")
    step("Tidak ada stok minus (harga pokok sementara)", not minus or minus.status == controls.OK,
        "Tidak ada stok minus." if not minus or minus.status == controls.OK else minus.summary,
        reverse("merchant-stock-minus"), "Lihat stok minus")
    step("SPT Masa PPN bulan ini sudah diperiksa", False,
        "Cocokkan PPN keluaran dan masukan sebelum periode dikunci.",
        reverse("merchant-spt-ppn") + f"?month={year}-{month:02d}", "Buka SPT")
    return steps, first, last - timedelta(days=1)
