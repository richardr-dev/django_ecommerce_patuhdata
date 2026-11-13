"""Pemeriksaan pembukuan: does everything agree with everything else?

Each check ties one part of the books to the documents or counts behind it (the ledger to the
invoices, bills, stock, bank statements and marketplace payouts) and reports ok / info / warn /
fail with the two figures and the difference. Nothing here writes; a failing check names the page
where it is fixed. A check that itself breaks reports "error" and never hides the others."""
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.urls import reverse

from .ledger import (
    CUSTOMER_CREDIT,
    GATEWAY,
    INVENTORY,
    MARKETPLACE,
    PAYABLE,
    RECEIVABLE,
    UNIDENTIFIED,
    account_balances,
    locked_through,
    unposted,
)
from .models import Bill, CashTransaction, Expense, JournalLine

logger = logging.getLogger(__name__)
OK, INFO, WARN, FAIL, ERROR = "ok", "info", "warn", "fail", "error"
ORDER = {FAIL: 0, ERROR: 1, WARN: 2, INFO: 3, OK: 4}
ZERO = Decimal(0)
TOLERANCE = Decimal(1)


@dataclass
class Check:
    key: str
    title: str
    status: str
    summary: str
    book: Decimal | None = None
    other: Decimal | None = None
    other_label: str = ""
    url: str = ""
    action: str = ""
    items: list = field(default_factory=list)

    @property
    def difference(self):
        return None if self.book is None or self.other is None else self.book - self.other


def _tie(key, title, book, other, other_label, *, url, action, ok, off, status_off=WARN):
    """Compare two figures: ok within a rupiah, else ``status_off`` with the explanation."""
    close = abs(book - other) <= TOLERANCE
    return Check(key, title, OK if close else status_off, ok if close else off, book, other,
        other_label, url, "" if close else action)


def _balances(store):
    return {a.code: a.balance for a in account_balances(store)}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def trial_balance(store, today, ledger):
    totals = JournalLine.objects.filter(entry__store=store).aggregate(
        d=Sum("debit", default=0), c=Sum("credit", default=0))
    return _tie("trial_balance", "Debit sama dengan kredit", totals["d"], totals["c"], "Total kredit",
        url=reverse("merchant-trial-balance"), action="Buka neraca saldo",
        ok="Seluruh jurnal seimbang.", off="Jurnal tidak seimbang: ada pencatatan yang rusak.",
        status_off=FAIL)


def unposted_documents(store, today, ledger):
    from apps.orders.models import PaymentTransaction, Refund
    from apps.tax.models import CreditNote, CustomerReceipt, Invoice
    from apps.tax.posting import UNPOSTED_STATUSES, booked_by_invoice
    found = {
        "invoice penjualan": [i for i in unposted(Invoice.objects.filter(store=store).exclude(
            status__in=UNPOSTED_STATUSES), "invoice") if booked_by_invoice(i)],
        "tagihan supplier": list(unposted(Bill.objects.filter(store=store).exclude(
            status=Bill.STATUS_VOID), "bill")),
        "transaksi kas & bank": list(unposted(CashTransaction.objects.filter(store=store,
            voided=False), "cash")),
        "nota kredit": list(unposted(CreditNote.objects.filter(store=store).exclude(
            status=CreditNote.STATUS_VOID), "credit_note")),
        "uang muka pelanggan": list(unposted(CustomerReceipt.objects.filter(store=store,
            voided=False, deposit__gt=0), "receipt_deposit")),
        "pembayaran pesanan": list(PaymentTransaction.objects.filter(order__store=store,
            kind="payment", status="succeeded", journal_entry__isnull=True)),
        "refund": list(Refund.objects.filter(order__store=store, status="succeeded",
            journal_entry__isnull=True)),
        "biaya operasional": list(Expense.objects.filter(store=store, journal_entry__isnull=True,
            approval_pending=False)),
    }
    counts = {label: len(rows) for label, rows in found.items() if rows}
    total = sum(counts.values())
    if not total:
        return Check("unposted", "Semua dokumen sudah masuk buku besar", OK,
            "Tidak ada invoice, tagihan, pembayaran, atau biaya yang tertinggal.")
    return Check("unposted", "Dokumen belum masuk buku besar", WARN,
        f"{total} dokumen belum dijurnal: " + ", ".join(f"{n} {label}" for label, n in counts.items())
        + ". Biasanya karena periode ditutup atau posting gagal; worker harian mengulanginya.",
        url=reverse("merchant-journal-list"), action="Posting yang tertinggal sekarang",
        items=[f"{n} {label}" for label, n in counts.items()])


def invoice_vs_journal(store, today, ledger):
    from apps.tax.models import Invoice
    posted = {row["entry__source_id"]: row["d"] for row in JournalLine.objects.filter(
        entry__store=store, entry__source_type="invoice", entry__superseded=False,
        account__code=RECEIVABLE).values("entry__source_id").annotate(d=Sum("debit"))}
    wrong = []
    for invoice in Invoice.objects.filter(store=store, pk__in=list(posted)):
        if abs(invoice.total - posted[str(invoice.pk)]) > TOLERANCE:
            wrong.append(f"{invoice.number}: invoice {invoice.total:,.0f}, jurnal "
                f"{posted[str(invoice.pk)]:,.0f}".replace(",", "."))
    if not wrong:
        return Check("invoice_journal", "Invoice sama dengan jurnalnya", OK,
            f"{len(posted)} invoice yang dijurnal cocok dengan totalnya.")
    return Check("invoice_journal", "Invoice tidak sama dengan jurnalnya", FAIL,
        f"{len(wrong)} invoice berbeda dari jurnalnya (diubah setelah dijurnal?).",
        url=reverse("merchant-sales-invoices"), action="Buka invoice", items=wrong[:10])


def invoice_paid_status(store, today, ledger):
    from apps.tax.posting import paid_elsewhere
    count = paid_elsewhere(store).count()
    if not count:
        return Check("invoice_status", "Status invoice sesuai pembayarannya", OK,
            "Tidak ada invoice terbuka untuk pesanan yang sudah dibayar lewat gateway/marketplace.")
    return Check("invoice_status", "Invoice terbuka padahal pesanannya sudah dibayar", WARN,
        f"{count} invoice masih berstatus belum dibayar, padahal pesanannya dibayar lewat gateway "
        "atau marketplace. Piutang pelanggan jadi tampak lebih besar dari kenyataan.",
        url=reverse("merchant-sales-invoices") + "?inv=awaiting", action="Perbaiki status")


def receivable(store, today, ledger):
    from apps.accounting import opening
    from apps.tax.receivables import aging
    _, totals = aging(store, today)
    book = ledger.get(RECEIVABLE, ZERO)
    check = _tie("receivable", "Piutang usaha sama dengan invoice terbuka", book, totals["total"],
        "Invoice terbuka", url=reverse("merchant-receivables"), action="Lihat piutang",
        ok="Piutang di buku sama dengan jumlah invoice yang belum dibayar.",
        off="Piutang di buku berbeda dari invoice terbuka.")
    if check.status == WARN and opening.existing(store) and book > totals["total"]:
        check.status = INFO
        check.summary = ("Selisih berasal dari piutang saldo awal yang belum dirinci per invoice; "
            "turun saat pelunasannya dicatat.")
    return check


def payable(store, today, ledger):
    from apps.accounting import opening
    from apps.accounting.payables import aging
    _, totals = aging(store, today)
    book = ledger.get(PAYABLE, ZERO)
    check = _tie("payable", "Utang usaha sama dengan tagihan terbuka", book, totals["total"],
        "Tagihan terbuka", url=reverse("merchant-payables"), action="Lihat utang",
        ok="Utang di buku sama dengan jumlah tagihan supplier yang belum dibayar.",
        off="Utang di buku berbeda dari tagihan terbuka.")
    if check.status == WARN and opening.existing(store) and book > totals["total"]:
        check.status = INFO
        check.summary = ("Selisih berasal dari utang saldo awal yang belum dirinci per tagihan; "
            "turun saat pelunasannya dicatat.")
    return check


def inventory_value(store, today, ledger):
    from . import inventory
    data = inventory.reconciliation(store)
    expected = data["shelf_value"] + data["in_transit"] - data["deficit_value"]
    check = _tie("inventory", "Persediaan sama dengan nilai stok", data["book"], expected,
        "Nilai stok + barang belum diterima − stok minus", url=reverse("merchant-inventory-value"),
        action="Buka nilai persediaan",
        ok="Persediaan di buku sama dengan nilai stok di gudang.",
        off="Persediaan di buku berbeda dari nilai stok (stok awal belum dicatat, atau HPP "
            "tanpa pergerakan stok).")
    check.book, check.other = data["book"], expected
    if data["missing_cost"] and check.status == OK:
        check.status, check.summary = INFO, (f"{data['missing_cost']} barang belum punya harga "
            "pokok, jadi dinilai Rp0 dan HPP penjualannya belum tercatat.")
    return check


def negative_inventory(store, today, ledger):
    book = ledger.get(INVENTORY, ZERO)
    if book >= 0:
        return Check("negative_inventory", "Persediaan tidak minus", OK,
            "Saldo Persediaan tidak negatif.")
    return Check("negative_inventory", "Persediaan di buku minus", FAIL,
        f"Persediaan Rp{book:,.0f}: HPP tercatat lebih besar dari stok yang masuk. Catat saldo "
        "awal persediaan atau cek barang tanpa pelacakan stok.".replace(",", "."), book, ZERO,
        "Seharusnya", reverse("merchant-inventory-value"), "Buka nilai persediaan")


def stock_movements(store, today, ledger):
    from apps.orders.stock import stock_audit
    drift = stock_audit(store)
    if not drift:
        return Check("stock_movements", "Stok sama dengan pergerakannya", OK,
            "Setiap angka stok dijelaskan oleh pergerakan stoknya.")
    return Check("stock_movements", "Stok tidak cocok dengan pergerakannya", WARN,
        f"{len(drift)} barang punya jumlah stok yang tidak dijelaskan riwayat pergerakannya. "
        "Jalankan: manage.py stock_audit --fix", url=reverse("merchant-stock"),
        action="Buka stok gudang",
        items=[f"{getattr(item, 'product', item)}: stok {recorded}, pergerakan {explained}"
            for item, recorded, explained in drift[:10]])


def cash_accounts(store, today, ledger):
    from . import bank
    out = []
    for account in bank.bank_accounts(store):
        summary = bank.reconciliation_summary(account)
        url = reverse("merchant-bank-account", args=[account.pk])
        title = f"Rekening {account.name} sama dengan bank"
        if summary["statement_balance"] is None:
            out.append(Check(f"bank_{account.pk}", title, INFO,
                "Belum ada mutasi bank yang diimpor, jadi belum bisa dicocokkan.",
                summary["book_balance"], None, "", url, "Impor mutasi"))
            continue
        book_then = bank.balances(store, [account], end=summary["statement_date"])[account.id]
        check = _tie(f"bank_{account.pk}", title, book_then, summary["statement_balance"],
            f"Saldo bank {summary['statement_date']:%d/%m/%Y}", url=url, action="Rekonsiliasi",
            ok="Saldo buku sama dengan saldo bank pada tanggal mutasi terakhir.",
            off="Saldo buku berbeda dari saldo bank pada tanggal mutasi terakhir.")
        if summary["unmatched_count"]:
            check.status = max((check.status, WARN), key=lambda s: -ORDER[s])
            check.summary += f" {summary['unmatched_count']} mutasi belum dicocokkan."
            check.action = "Cocokkan mutasi"
        out.append(check)
    return out


def held_by_providers(store, today, ledger):
    from .payouts import outstanding_by_provider
    waiting = sum((row["total"] for row in outstanding_by_provider(store)), ZERO)
    book = ledger.get(GATEWAY, ZERO) + ledger.get(MARKETPLACE, ZERO)
    check = _tie("providers", "Dana di gateway dan marketplace sama dengan yang belum cair", book,
        waiting, "Pembayaran belum dicairkan", url=reverse("merchant-payouts"),
        action="Cocokkan pencairan",
        ok="Piutang gateway/marketplace sama dengan pembayaran yang belum dicairkan.",
        off="Selisih biasanya dari refund, biaya, atau pencairan yang belum diimpor.")
    return check


def customer_credit(store, today, ledger):
    from apps.tax.models import CustomerCredit
    docs = CustomerCredit.objects.filter(store=store, voided=False).aggregate(
        total=Sum("amount", default=0))["total"]
    return _tie("customer_credit", "Kredit pelanggan sama dengan rinciannya",
        ledger.get(CUSTOMER_CREDIT, ZERO), docs, "Rincian per pelanggan",
        url=reverse("merchant-customer-credit"), action="Lihat kredit pelanggan",
        ok="Uang muka dan kredit pelanggan di buku sama dengan rinciannya.",
        off="Kredit pelanggan di buku berbeda dari rinciannya.")


def unidentified(store, today, ledger):
    amount = ledger.get(UNIDENTIFIED, ZERO)
    if abs(amount) <= TOLERANCE:
        return Check("unidentified", "Tidak ada pembayaran tak teridentifikasi", OK,
            "Setiap pembayaran yang masuk sudah cocok dengan pesanannya.")
    return Check("unidentified", "Ada pembayaran yang belum teridentifikasi", WARN,
        "Uang diterima untuk pesanan yang tidak bisa menampungnya (kedaluwarsa, jumlah beda). "
        "Refund atau cocokkan.", amount, ZERO, "Seharusnya", reverse("merchant-integrations"),
        "Periksa pembayaran")


def paid_orders_without_invoice(store, today, ledger):
    from apps.orders.models import Order
    count = Order.objects.filter(store=store, invoice__isnull=True).exclude(
        status__in=["pending", "payment_failed", "cancelled", "expired"]).count()
    if not count:
        return Check("no_invoice", "Setiap pesanan dibayar punya invoice", OK,
            "Tidak ada pesanan dibayar tanpa invoice.")
    return Check("no_invoice", "Pesanan dibayar tanpa invoice", WARN,
        f"{count} pesanan belum punya invoice. Penjualannya tetap tercatat di buku dari "
        "pembayarannya, tapi belum ada dokumen invoice untuk pelanggan.",
        url=reverse("merchant-sales-invoices") + "?inv=none", action="Terbitkan invoice")


def unmapped_skus(store, today, ledger):
    from apps.channels.models import UnmappedSku
    count = UnmappedSku.objects.filter(store=store, resolved=False).count()
    if not count:
        return Check("unmapped", "Semua SKU marketplace dikenali", OK,
            "Tidak ada SKU marketplace yang belum dipetakan.")
    return Check("unmapped", "SKU marketplace belum dipetakan", WARN,
        f"{count} SKU tidak cocok dengan produk: pesanannya ditolak atau memakai produk "
        "sementara, sehingga stok dan HPP-nya tidak benar.",
        url=reverse("merchant-sku-map"), action="Petakan SKU")


def sold_without_cost(store, today, ledger):
    from datetime import timedelta

    from apps.orders.models import OrderItem
    since = today - timedelta(days=90)
    count = (OrderItem.objects.filter(order__store=store, order__created_at__date__gte=since,
        unit_cost__isnull=True, product__cost_price__isnull=True)
        .exclude(order__status__in=["pending", "payment_failed", "cancelled"])
        .aggregate(n=Count("order", distinct=True))["n"])
    if not count:
        return Check("no_cost", "Penjualan 90 hari terakhir punya harga pokok", OK,
            "Semua barang yang terjual punya harga pokok, jadi laba kotor akurat.")
    return Check("no_cost", "Penjualan tanpa harga pokok", WARN,
        f"{count} pesanan 90 hari terakhir berisi barang tanpa harga pokok: HPP-nya Rp0 sehingga "
        "laba terlalu besar.", url=reverse("merchant-entity-list", args=["products"]),
        action="Isi harga pokok")


def marketplace_income(store, today, ledger):
    from apps.channels.models import ChannelOrder
    missing = ChannelOrder.objects.filter(channel__store=store, income__isnull=True).exclude(
        order__status__in=["cancelled", "pending"]).count()
    if not missing:
        return Check("income", "Biaya marketplace lengkap", OK,
            "Setiap pesanan marketplace punya laporan penghasilan.")
    return Check("income", "Pesanan marketplace tanpa laporan penghasilan", INFO,
        f"{missing} pesanan belum punya data biaya marketplace (komisi, ongkir, voucher), jadi "
        "margin per channel belum memperhitungkannya.", url=reverse("merchant-income-import"),
        action="Impor penghasilan")


def period_lock(store, today, ledger):
    locked = locked_through(store)
    if locked:
        return Check("lock", "Periode ditutup", OK,
            f"Pembukuan sampai {locked:%d/%m/%Y} sudah ditutup dan tidak bisa berubah.")
    return Check("lock", "Belum ada periode yang ditutup", INFO,
        "Tutup buku setelah laporan pajak sebulan selesai, agar angkanya tidak berubah lagi.",
        url=reverse("merchant-year-close"), action="Tutup buku")


def withholding_tie(store, today, ledger):
    from .models import Withholding
    owed = sum((w.amount for w in Withholding.objects.filter(store=store, deposit__isnull=True)), ZERO)
    book = ledger.get("2150", ZERO)
    check = _tie("withholding", "Utang PPh sama dengan PPh yang belum disetor", book, owed,
        "PPh belum disetor di register", url=reverse("merchant-withholdings"), action="Buka PPh dipotong",
        ok="Utang PPh di buku sama dengan PPh dipotong yang belum disetor.",
        off="Saldo Utang PPh berbeda dari register PPh dipotong (ada jurnal manual atau pemotongan tanpa bukti potong).")
    old = Withholding.objects.filter(store=store, deposit__isnull=True,
        date__lt=today.replace(day=1) - timedelta(days=1)).count()
    if old and check.status == OK:
        check.status, check.summary = INFO, f"{old} pemotongan dari bulan sebelumnya belum disetor (batas setor tanggal 10 bulan berikutnya)."
    return check


def intercompany_tie(store, today, ledger):
    """Between the Organization's brands, what one is owed equals what the other owes."""
    from .intercompany import group_balances
    if not store.organization_id or store.organization.stores.count() < 2:
        return Check("intercompany", "Transfer antar brand", INFO, "Hanya ada satu brand di organisasi ini.")
    receivable, payable = group_balances(store.organization)
    return _tie("intercompany", "Piutang dan utang antar brand saling hapus", receivable, payable,
        "Utang antar perusahaan", url=reverse("merchant-intercompany"), action="Buka transfer antar brand",
        ok="Piutang antar perusahaan sama dengan utang antar perusahaan di seluruh brand.",
        off="Piutang dan utang antar brand tidak sama: ada pembayaran antar brand yang baru dicatat di satu sisi.",
        status_off=INFO)


def negative_stock(store, today, ledger):
    """Open stok minus: sales beyond the shelf whose cost of goods is still provisional."""
    from apps.orders.deficits import summary
    minus = summary(store)
    if not minus["units"]:
        return Check("negative_stock", "Tidak ada stok minus", OK,
            "Tidak ada penjualan melebihi stok yang masih terbuka.")
    return Check("negative_stock", "Stok minus dengan harga pokok sementara", WARN,
        f"{minus['units']} unit dari {minus['items']} barang dijual melebihi stok (nilai sementara "
        f"Rp{minus['value']:,.0f}). HPP-nya dikoreksi saat barang diterima.".replace(",", "."),
        url=reverse("merchant-stock-minus"), action="Buka stok minus")


def grni_tie(store, today, ledger):
    """Barang diterima belum ditagih equals the value of receipts still waiting for their bill."""
    from .models import GoodsReceipt
    waiting = sum((r.value for r in GoodsReceipt.objects.filter(store=store, bill__isnull=True
        ).prefetch_related("lines")), ZERO)
    check = _tie("grni", "GRNI sama dengan penerimaan yang belum ditagih", ledger.get("2350", ZERO), waiting,
        "Penerimaan belum ditagih", url=reverse("merchant-goods-receipts"), action="Buka penerimaan barang",
        ok="Saldo GRNI sama dengan nilai penerimaan barang yang belum ditagih.",
        off="Saldo GRNI berbeda dari penerimaan yang belum ditagih (ada jurnal manual ke akun GRNI atau tagihan dibatalkan tanpa membuka penerimaan).")
    from datetime import timedelta as _td

    from .models import GoodsReceipt as _GR
    old = _GR.objects.filter(store=store, bill__isnull=True, received_on__lt=today - _td(days=30)).count()
    if old and check.status == OK:
        check.status, check.summary = INFO, f"{old} penerimaan barang lebih dari 30 hari belum ada tagihannya."
    return check


def year_close_tie(store, today, ledger):
    """A closed year's profit still equals what was moved to Laba ditahan (nothing slipped in
    after the closing)."""
    from . import closing
    from .ledger import RETAINED, YEAR_CLOSE
    from .models import JournalLine
    off = []
    for year in closing.closed_years(store):
        moved = sum((line.credit - line.debit for line in JournalLine.objects.filter(
            entry__store=store, entry__source_type=YEAR_CLOSE, entry__source_id=str(year),
            entry__superseded=False, account__code=RETAINED)), ZERO)
        profit = closing.profit_of(store, year)
        if abs(moved - profit) > TOLERANCE:
            off.append((year, moved, profit))
    if off:
        year, moved, profit = off[0]
        return Check("year_close", "Laba tahun yang ditutup berubah", FAIL,
            f"Tahun {year}: laba ke Laba ditahan Rp{moved:,.0f}, laba laporan Rp{profit:,.0f}. "
            "Ada jurnal bertanggal di tahun itu setelah penutupan.".replace(",", "."),
            moved, profit, "Laba laporan", reverse("merchant-year-close"), "Buka Tutup buku")
    return Check("year_close", "Penutupan tahun cocok dengan laba", OK,
        "Laba setiap tahun yang ditutup sama dengan yang dipindahkan ke Laba ditahan.")


def budget_control(store, today, ledger):
    from . import budgets
    alerts = budgets.alerts(store, today)
    if not alerts["over"] and not alerts["warn"]:
        return Check("budget", "Biaya dalam anggaran", OK, "Tidak ada pos biaya yang melewati anggaran bulan ini.")
    worst = alerts["over"] or alerts["warn"]
    check = Check("budget", "Biaya melewati anggaran", WARN if alerts["over"] else INFO,
        f"{len(alerts['over'])} pos melebihi anggaran dan {len(alerts['warn'])} hampir habis bulan ini.",
        url=reverse("merchant-budget"), action="Buka anggaran")
    check.items = [f"{r['name']}: {r['pct']}% dari anggaran" for r in worst][:10]
    return check


def duplicate_bills(store, today, ledger):
    """The same supplier bill entered twice is the commonest way to pay a supplier twice."""
    from django.db.models import Count
    bills = Bill.objects.filter(store=store).exclude(status=Bill.STATUS_VOID)
    same_number = (bills.exclude(number="").values("supplier", "number").annotate(n=Count("id")).filter(n__gt=1))
    same_amount = (bills.values("supplier", "bill_date", "total").annotate(n=Count("id")).filter(n__gt=1))
    items = []
    for row in same_number:
        b = bills.filter(supplier=row["supplier"], number=row["number"]).select_related("supplier").first()
        items.append(f"{b.supplier.name}: nomor faktur {row['number']} tercatat {row['n']}×")
    for row in same_amount:
        b = bills.filter(supplier=row["supplier"], bill_date=row["bill_date"], total=row["total"]).select_related("supplier").first()
        text = f"{b.supplier.name}: Rp{row['total']:,.0f} pada {row['bill_date']:%d %b %Y} tercatat {row['n']}×".replace(",", ".")
        if text not in items:
            items.append(text)
    if not items:
        return Check("duplicate_bills", "Tagihan supplier ganda", OK, "Tidak ada tagihan supplier yang tercatat dua kali.")
    check = Check("duplicate_bills", "Tagihan supplier mungkin ganda", WARN,
        f"{len(items)} tagihan terlihat sama. Batalkan yang kedua sebelum dibayar.",
        url=reverse("merchant-bills"), action="Buka tagihan")
    check.items = items[:10]
    return check


def unusual_expenses(store, today, ledger):
    """A cost far above what the store usually pays is worth a second look."""
    import statistics
    since = today - timedelta(days=90)
    rows = list(Expense.objects.filter(store=store, date__gte=since).values_list("amount", "description", "date"))
    if len(rows) < 6:
        return Check("unusual_expenses", "Biaya tidak biasa", OK, "Belum cukup biaya untuk dibandingkan.")
    median = statistics.median(float(a) for a, _, _ in rows)
    odd = [(a, d, when) for a, d, when in rows if float(a) > 3 * median and a >= 1_000_000]
    if not odd:
        return Check("unusual_expenses", "Biaya tidak biasa", OK, "Tidak ada biaya yang jauh di atas kebiasaan.")
    check = Check("unusual_expenses", "Biaya jauh di atas biasanya", INFO,
        f"{len(odd)} biaya lebih dari 3× biaya tengah Anda (Rp{median:,.0f}) dalam 90 hari.".replace(",", "."),
        url=reverse("merchant-expenses"), action="Buka biaya")
    check.items = [f"{when:%d %b}: {desc} Rp{a:,.0f}".replace(",", ".") for a, desc, when in odd[:10]]
    return check


def large_manual_journals(store, today, ledger):
    """Manual journals bypass every document, so the big ones get listed for review."""
    from .models import JournalEntry
    since = today - timedelta(days=30)
    entries = JournalEntry.objects.filter(store=store, source_type="journal", date__gte=since,
        superseded=False).prefetch_related("lines")
    big = [(e, sum(line.debit for line in e.lines.all())) for e in entries]
    big = [(e, total) for e, total in big if total >= 10_000_000]
    if not big:
        return Check("manual_journals", "Jurnal manual besar", OK, "Tidak ada jurnal manual besar dalam 30 hari.")
    check = Check("manual_journals", "Jurnal manual besar untuk ditinjau", INFO,
        f"{len(big)} jurnal manual di atas Rp10 juta dalam 30 hari terakhir.",
        url=reverse("merchant-journal-list"), action="Buka jurnal")
    check.items = [f"{e.date:%d %b}: {e.memo} Rp{total:,.0f}".replace(",", ".") for e, total in big[:10]]
    return check


def backdated_entries(store, today, ledger):
    """Entries recorded this week but dated more than a month back change a period people may
    already have reported on."""
    from .models import JournalEntry
    week = today - timedelta(days=7)
    old = JournalEntry.objects.filter(store=store, created_at__date__gte=week,
        date__lt=today - timedelta(days=37)).exclude(source_type__in=["year_close", "opening"]).count()
    if not old:
        return Check("backdated", "Jurnal bertanggal mundur", OK, "Tidak ada jurnal baru yang bertanggal jauh ke belakang.")
    return Check("backdated", "Jurnal bertanggal mundur", INFO,
        f"{old} jurnal dicatat minggu ini dengan tanggal lebih dari sebulan lalu. Pastikan periodenya belum dilaporkan.",
        url=reverse("merchant-journal-list"), action="Buka jurnal")


def voids_after_payment(store, today, ledger):
    """An invoice cancelled after money was received needs an explanation."""
    from apps.tax.models import Invoice, InvoicePayment
    paid = set(InvoicePayment.objects.filter(invoice__store=store, invoice__status=Invoice.STATUS_VOID
        ).values_list("invoice__number", flat=True))
    if not paid:
        return Check("voids_after_payment", "Invoice dibatalkan setelah dibayar", OK,
            "Tidak ada invoice yang dibatalkan setelah menerima pembayaran.")
    check = Check("voids_after_payment", "Invoice dibatalkan setelah dibayar", WARN,
        f"{len(paid)} invoice dibatalkan padahal sudah ada pembayaran. Pastikan uangnya dikembalikan.",
        url=reverse("merchant-sales-invoices") + "?inv=void", action="Buka invoice batal")
    check.items = sorted(paid)[:10]
    return check


CHECKS = [trial_balance, unposted_documents, invoice_vs_journal, invoice_paid_status, receivable, payable,
    inventory_value, negative_inventory, stock_movements, cash_accounts, held_by_providers,
    customer_credit, unidentified, paid_orders_without_invoice, unmapped_skus, sold_without_cost,
    marketplace_income, period_lock, budget_control, duplicate_bills, unusual_expenses,
    large_manual_journals, backdated_entries, voids_after_payment, withholding_tie, year_close_tie, grni_tie, intercompany_tie, negative_stock]


def run_checks(store, today):
    """Every check, worst first within the order they are listed. A broken check is an ``error``
    row, never an exception."""
    ledger = _balances(store)
    results = []
    for check in CHECKS:
        try:
            found = check(store, today, ledger)
        except Exception:
            logger.exception("books check %s failed", check.__name__)
            results.append(Check(check.__name__, check.__name__.replace("_", " ").capitalize(),
                ERROR, "Pemeriksaan ini gagal dijalankan. Hubungi pengelola."))
            continue
        results.extend(found if isinstance(found, list) else [found])
    return results


def summarize(results):
    counts = {status: sum(1 for r in results if r.status == status)
        for status in (OK, INFO, WARN, FAIL, ERROR)}
    worst = min((r.status for r in results), key=lambda s: ORDER[s], default=OK)
    return {**counts, "attention": counts[WARN] + counts[FAIL] + counts[ERROR], "worst": worst,
        "total": len(results)}
