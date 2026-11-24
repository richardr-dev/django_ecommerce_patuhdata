"""Pemeriksaan pembukuan: does everything agree with everything else?

Each check ties one part of the books to the documents or counts behind it (the ledger to the
invoices, bills, stock, bank statements and marketplace payouts) and reports ok / info / warn /
fail with the two figures and the difference. Nothing here writes; a failing check names the page
where it is fixed. A check that itself breaks reports "error" and never hides the others."""
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.urls import reverse
from django.utils import timezone

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


def invoice_open_balance(store, today, ledger):
    """Invoice by invoice: the Piutang the books hold for it against what it says is still owed. The
    receivable check below compares only the totals; this one names the invoice that is off."""
    from collections import defaultdict

    from apps.tax.models import CreditNote, Invoice, InvoicePayment
    from apps.tax.posting import UNPOSTED_STATUSES, booked_by_invoice
    invoices = [i for i in Invoice.objects.filter(store=store).exclude(status__in=UNPOSTED_STATUSES)
        .select_related("order") if booked_by_invoice(i)]
    if not invoices:
        return Check("invoice_open_balance", "Piutang tiap invoice sesuai", OK, "Belum ada invoice yang dijurnal.")
    owner = {("invoice", str(i.pk)): i.pk for i in invoices}
    ids = [i.pk for i in invoices]
    skipped = set()
    for note in CreditNote.objects.filter(invoice_id__in=ids).exclude(status=CreditNote.STATUS_VOID):
        owner[("credit_note", str(note.pk))] = note.invoice_id
        if note.excess_action == CreditNote.EXCESS_PLATFORM:
            skipped.add(note.invoice_id)   # the marketplace pays the buyer: not through Piutang usaha
    for pay_id, inv_id in InvoicePayment.objects.filter(invoice_id__in=ids).values_list("pk", "invoice_id"):
        owner[("invoice_payment", str(pay_id))] = inv_id
    from apps.orders.models import PaymentTransaction, Refund
    for pk, inv_id in PaymentTransaction.objects.filter(order__invoice__in=ids).values_list("pk", "order__invoice"):
        owner[("payment", str(pk))] = inv_id
    for pk, inv_id in Refund.objects.filter(order__invoice__in=ids).values_list("pk", "order__invoice"):
        owner[("refund", str(pk))] = inv_id
    moved = defaultdict(Decimal)
    kinds = {t for t, _ in owner}
    lines = JournalLine.objects.filter(entry__store=store, account__code=RECEIVABLE)
    rows = list(lines.filter(entry__source_type__in=kinds).values_list(
        "entry__source_type", "entry__source_id", "debit", "credit"))
    # An edited invoice or a corrected payment is reversed and booked again: the reversal counts for the
    # document it reverses, or the invoice would show its old amount twice.
    rows += list(lines.filter(entry__source_type="reversal", entry__reverses__source_type__in=kinds).values_list(
        "entry__reverses__source_type", "entry__reverses__source_id", "debit", "credit"))
    for source_type, source_id, debit, credit in rows:
        inv_id = owner.get((source_type, source_id))
        if inv_id is not None:
            moved[inv_id] += debit - credit
    wrong = []
    for invoice in invoices:
        if invoice.pk in skipped or invoice.pk not in moved:
            continue
        if abs(moved[invoice.pk] - invoice.amount_due) > TOLERANCE:
            wrong.append(f"{invoice.number}: invoice {invoice.amount_due:,.0f}, buku {moved[invoice.pk]:,.0f}"
                .replace(",", "."))
    if not wrong:
        return Check("invoice_open_balance", "Piutang tiap invoice sesuai", OK,
            f"Sisa tagihan {len(moved)} invoice sama dengan piutangnya di buku besar.")
    return Check("invoice_open_balance", "Ada invoice yang piutangnya tidak sama dengan sisa tagihan", WARN,
        f"{len(wrong)} invoice berbeda: sisa tagihan di invoice tidak sama dengan piutangnya di buku besar. "
        "Buka invoice untuk melihat jurnalnya.", url=reverse("merchant-sales-invoices"), action="Buka invoice",
        items=wrong[:10])


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
    expected = data["shelf_value"] + data["in_transit"] + data["moving"] - data["deficit_value"]
    check = _tie("inventory", "Persediaan sama dengan nilai stok", data["book"], expected,
        "Nilai stok + barang belum diterima + kiriman antar gudang − stok minus", url=reverse("merchant-inventory-value"),
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
    from apps.orders.variant_stock import drift as location_drift
    drift = stock_audit(store)
    spread = location_drift(store)
    if spread and not drift:
        return Check("stock_movements", "Stok varian per gudang tidak sama dengan totalnya", WARN,
            f"{len(spread)} varian punya jumlah per gudang yang tidak sama dengan stok totalnya (diubah di luar "
            "pergerakan stok).", url=reverse("merchant-stock"), action="Buka stok gudang",
            items=[f"{variant}: total {total}, per gudang {held}" for variant, total, held in spread[:10]])
    if not drift:
        return Check("stock_movements", "Stok sama dengan pergerakannya", OK,
            "Setiap angka stok dijelaskan oleh pergerakan stoknya.")
    return Check("stock_movements", "Stok tidak cocok dengan pergerakannya", WARN,
        f"{len(drift)} barang punya jumlah stok yang tidak dijelaskan riwayat pergerakannya. "
        "Jalankan: manage.py stock_audit --fix", url=reverse("merchant-stock"),
        action="Buka stok gudang",
        items=[f"{getattr(item, 'product', item)}: stok {recorded}, pergerakan {explained}"
            for item, recorded, explained in drift[:10]])


def stock_changes_booked(store, today, ledger):
    """Standard inventory flow: once the books run, every stock change comes from a document that books its value
    (receipt, sale, transfer, stok opname, Penyesuaian stok). Flags quantities typed in without one, and
    adjustments whose journal is missing or does not carry their value."""
    from apps.orders.models import InventoryMovement

    from .models import JournalEntry, StockAdjustment
    first = JournalEntry.objects.filter(store=store).order_by("created_at").values_list("created_at", flat=True).first()
    loose = InventoryMovement.objects.none()
    if first is not None:
        loose = InventoryMovement.objects.filter(store=store, kind__in=("adjust", "opname"), created_at__gt=first,
            source_type__in=("", "setup", "product_import")).exclude(new_on_hand=F("previous_on_hand"))
    posted = StockAdjustment.objects.filter(store=store, status=StockAdjustment.POSTED).exclude(value=0)
    booked = dict(JournalLine.objects.filter(entry__store=store, entry__source_type="stock_adjustment",
        entry__superseded=False, account__code=INVENTORY).values("entry__source_id")
        .annotate(total=Sum("debit") - Sum("credit")).values_list("entry__source_id", "total"))
    wrong = [a.number for a in posted if booked.get(str(a.pk), ZERO) != a.value]
    count = loose.count()
    if not count and not wrong:
        return Check("stock_changes_booked", "Setiap perubahan stok lewat dokumen", OK,
            "Sejak pembukuan berjalan, setiap perubahan stok berasal dari dokumen yang ikut membukukan Persediaan.")
    items = [f"Penyesuaian {number}: jurnal Persediaan tidak sama dengan nilainya" for number in wrong[:10]]
    items += [f"{move.created_at:%d %b %Y} · {move.inventory_level.product.name if move.inventory_level_id else move.variant}: "
        f"{move.previous_on_hand} → {move.new_on_hand} ({move.reason})" for move in loose.select_related(
            "inventory_level__product", "variant")[:10]]
    return Check("stock_changes_booked", "Perubahan stok tanpa jurnal", WARN,
        (f"{count} perubahan stok diketik langsung tanpa dokumen setelah pembukuan berjalan" if count else "")
        + ("; " if count and wrong else "") + (f"{len(wrong)} penyesuaian stok tanpa jurnal yang cocok" if wrong else "")
        + ". Nilainya tampil sebagai selisih di Nilai persediaan; catat perubahan berikutnya lewat Penyesuaian stok.",
        url=reverse("merchant-stock-adjustments"), action="Buka penyesuaian stok", items=items)


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
    from .marketplace_adjustments import open_total
    from .payouts import outstanding_by_provider
    # Open selisih/klaim change what the marketplace owes until a payout or transfer settles them.
    waiting = sum((row["total"] for row in outstanding_by_provider(store)), ZERO) + open_total(store)
    book = ledger.get(GATEWAY, ZERO) + ledger.get(MARKETPLACE, ZERO)
    check = _tie("providers", "Dana di gateway dan marketplace sama dengan yang belum cair", book,
        waiting, "Pembayaran belum dicairkan", url=reverse("merchant-payouts"),
        action="Cocokkan pencairan",
        ok="Piutang gateway/marketplace sama dengan pembayaran yang belum dicairkan.",
        off="Selisih biasanya dari refund, biaya, atau pencairan yang belum diimpor.")
    return check


def customer_credit(store, today, ledger):
    from apps.tax.models import CustomerCredit
    totals = CustomerCredit.objects.filter(store=store, voided=False).aggregate(
        total=Sum("amount", default=0), vat=Sum("vat", default=0))
    docs = totals["total"] - totals["vat"]  # PPN inside a taxed uang muka sits in PPN keluaran, not 2400
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


def payroll_tie(store, today, ledger):
    """Utang gaji equals the take-home pay of posted runs not yet paid; Utang BPJS what is not yet paid to BPJS."""
    from .ledger import BPJS_PAYABLE, WAGES_PAYABLE
    from .payroll import owed
    wages, bpjs = owed(store)
    check = _tie("payroll", "Utang gaji dan BPJS sama dengan penggajian", ledger.get(WAGES_PAYABLE, ZERO)
        + ledger.get(BPJS_PAYABLE, ZERO), wages + bpjs, "Gaji dan BPJS belum dibayar di penggajian",
        url=reverse("merchant-payroll"), action="Buka penggajian",
        ok="Utang gaji dan Utang BPJS di buku sama dengan yang belum dibayar di penggajian.",
        off="Utang gaji atau Utang BPJS berbeda dari penggajian (ada jurnal manual ke akun 2160/2170).")
    from .models import PayrollRun
    late = list(PayrollRun.objects.filter(store=store, status=PayrollRun.POSTED, posted_on__lt=today - timedelta(days=7)))
    if late and check.status == OK:
        check.status = INFO
        check.summary = f"{len(late)} penggajian sudah diposting lebih dari seminggu tetapi gajinya belum dibayar."
        check.items = [f"{run.number} ({run.period:%m/%Y})" for run in late][:10]
    return check


def marketplace_pph22_tie(store, today, ledger):
    """PPh 22 dipungut marketplace (1525) equals what the imported income reports say the marketplaces collected,
    for a store on the general rates (under PPh Final UMKM it is an expense, 6990, and nothing is carried)."""
    from apps.channels.models import ChannelOrderIncome
    from apps.tax.services import tax_profile

    from .ledger import MARKETPLACE_PPH22
    collected = ChannelOrderIncome.objects.filter(channel_order__channel__store=store).exclude(
        payout_reference="").aggregate(total=Sum("pph22"))["total"] or ZERO
    book = ledger.get(MARKETPLACE_PPH22, ZERO)
    if tax_profile(store).pph_regime == "umkm_final":
        if not book:
            return Check("marketplace_pph22", "PPh 22 dipungut marketplace", OK,
                "PPh Final UMKM: PPh 22 dari marketplace dicatat sebagai bagian PPh final (6990), bukan kredit pajak.")
        collected = ZERO  # whatever sits in 1525 came from a time on the general rates or a manual journal
    return _tie("marketplace_pph22", "PPh 22 dipungut marketplace sama dengan laporan penghasilan", book, collected,
        "PPh 22 di laporan penghasilan marketplace", url=reverse("merchant-marketplace-tax"),
        action="Buka paket pajak marketplace",
        ok="Akun 1525 sama dengan PPh 22 yang dipungut marketplace menurut laporan penghasilan.",
        off="Akun 1525 berbeda dari PPh 22 di laporan penghasilan (jurnal manual, atau rezim pajak berubah setelah impor).")


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


def lots_tie(store, today, ledger):
    """Lot-tracked items: the units in their lots equal the units held, and no lot still held is past its date."""
    from apps.orders import lots
    off = lots.mismatches(store)
    expired = lots.expiring(store, today=today)["expired"]
    if off:
        names = ", ".join(f"{variant or product.name} (lot {lotted}, stok {held})" for product, variant, lotted, held
            in off[:5])
        return Check("lots", "Lot tidak sama dengan stok", WARN, f"Jumlah di lot berbeda dari stok untuk {names}. "
            "Biasanya karena stok berubah sebelum pelacakan lot dinyalakan; koreksi lewat stok opname.",
            url=reverse("merchant-lots"), action="Buka lot & kedaluwarsa")
    if expired:
        units = sum(lot.quantity for lot in expired)
        return Check("lots", "Ada lot yang sudah kedaluwarsa", WARN, f"{len(expired)} lot ({units} unit) sudah lewat "
            "tanggal kedaluwarsa dan masih tercatat di stok. Pisahkan lalu catat lewat Penyesuaian stok (rusak).",
            url=reverse("merchant-lots") + "?status=expired", action="Lihat lot kedaluwarsa")
    return Check("lots", "Lot sama dengan stok", OK, "Lot setiap barang yang dilacak sama dengan stoknya.")


def purchase_orders_late(store, today, ledger):
    """Orders whose goods were expected by now and are not all in."""
    from . import purchasing
    late = list(purchasing.overdue(store, today))
    if not late:
        return Check("po_late", "Pesanan pembelian tepat waktu", OK, "Tidak ada pesanan pembelian yang terlambat.")
    return Check("po_late", "Pesanan pembelian terlambat", INFO,
        f"{len(late)} pesanan melewati tanggal perkiraan tiba dan belum diterima semua "
        f"({', '.join(po.number for po in late[:3])}).", url=reverse("merchant-purchase-orders"),
        action="Buka pesanan pembelian")


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


def landed_cost_tie(store, today, ledger):
    """Biaya perolehan belum dialokasikan (1460) equals the freight/duty bills not yet spread over receipts."""
    from .landed_costs import waiting
    return _tie("landed_cost", "Biaya perolehan belum dialokasikan sama dengan tagihan ongkos angkut yang menunggu",
        ledger.get("1460", ZERO), waiting(store), "Tagihan belum dialokasikan", url=reverse("merchant-landed-costs"),
        action="Alokasikan ke penerimaan",
        ok="Saldo 1460 sama dengan tagihan ongkos angkut / bea masuk yang belum dialokasikan.",
        off="Saldo 1460 berbeda dari tagihan ongkos angkut yang belum dialokasikan (jurnal manual ke akun 1460, "
            "atau tagihan dibatalkan setelah dialokasikan).")


def delivered_tie(store, today, ledger):
    """Barang terkirim belum ditagih equals the cost of surat jalan not yet on an invoice."""
    from apps.tax.sales_orders import delivered_not_invoiced
    waiting = delivered_not_invoiced(store)
    return _tie("delivered", "Barang terkirim belum ditagih sama dengan surat jalan yang belum diinvoice",
        ledger.get("1410", ZERO), waiting, "Surat jalan belum ditagih", url=reverse("merchant-deliveries"),
        action="Buka surat jalan",
        ok="Saldo 1410 sama dengan harga pokok surat jalan yang belum ditagih.",
        off="Saldo 1410 berbeda dari surat jalan yang belum ditagih (jurnal manual ke akun itu, atau surat jalan belum terposting).")


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
        date__lt=today - timedelta(days=37)).exclude(source_type__in=["year_close", "opening"])
    # Saldo awal per invoice / per tagihan sit on the books' first day by design.
    from apps.tax.models import Invoice
    old = old.exclude(source_type="invoice", source_id__in=[str(pk) for pk in Invoice.objects.filter(
        store=store, order__order_type="opening").values_list("pk", flat=True)])
    old = old.exclude(source_type="bill", source_id__in=[str(pk) for pk in Bill.objects.filter(
        store=store, is_opening=True).values_list("pk", flat=True)]).count()
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


def pos_receipts(store, today, ledger):
    """Every register receipt is a paid invoice with a live entry, and card and QRIS money does not
    sit with the acquirer for days."""
    from apps.pos.models import PosSale
    from apps.tax.models import Invoice, InvoicePayment

    from .ledger import EDC_CLEARING, unposted
    sales = PosSale.objects.filter(store=store, status=PosSale.PAID)
    if not sales.exists():
        return Check("pos_receipts", "Struk kasir sama dengan invoice lunas", OK, "Belum ada penjualan kasir.")
    url = reverse("merchant-pos")
    open_ones = list(sales.exclude(invoice__status=Invoice.STATUS_PAID).values_list("number", flat=True)[:10])
    missing = [invoice.number for invoice in unposted(Invoice.objects.filter(pos_sale__in=sales), "invoice")][:10]
    if open_ones or missing:
        check = Check("pos_receipts", "Struk kasir sama dengan invoice lunas", FAIL,
            f"{len(open_ones)} struk belum lunas di buku dan {len(missing)} belum terjurnal. "
            "Jalankan posting ulang; bila tetap ada, periksa struknya.", url=url, action="Buka kasir")
        check.items = open_ones + missing
        return check
    waiting = ledger.get(EDC_CLEARING, ZERO)
    old = InvoicePayment.objects.filter(invoice__store=store, method__in=InvoicePayment.CLEARING_METHODS,
        paid_on__lt=today - timedelta(days=3)).exists()
    if waiting > TOLERANCE and old:
        return Check("pos_receipts", "Struk kasir sama dengan invoice lunas", INFO,
            "Semua struk lunas dan terjurnal. Piutang EDC / QRIS masih bersaldo dari transaksi lebih dari "
            "3 hari lalu: cocokkan pencairannya di rekening bank.", waiting, None, "",
            reverse("merchant-bank-accounts"), "Buka kas & bank")
    return Check("pos_receipts", "Struk kasir sama dengan invoice lunas", OK,
        "Semua struk kasir sudah menjadi invoice lunas dan terjurnal.")


def pos_shifts(store, today, ledger):
    """No shift is left open overnight: an open drawer is cash nobody has counted."""
    from apps.pos.models import PosShift
    stale = [shift for shift in PosShift.objects.filter(register__store=store, status=PosShift.OPEN
        ).select_related("register__outlet") if timezone.localdate(shift.opened_at) < today]
    if not stale:
        return Check("pos_shifts", "Shift kasir ditutup setiap hari", OK, "Tidak ada shift kasir yang terbuka dari hari sebelumnya.")
    check = Check("pos_shifts", "Shift kasir ditutup setiap hari", WARN,
        f"{len(stale)} shift masih terbuka sejak hari sebelumnya. Hitung laci dan tutup shiftnya.",
        url=reverse("merchant-pos"), action="Buka kasir")
    check.items = [f"{shift.register.outlet.name} {shift.register.code} · dibuka {timezone.localtime(shift.opened_at):%d/%m %H.%M}"
        for shift in stale[:10]]
    return check


def pos_voids(store, today, ledger):
    """A cashier who voids several struk in a day is worth a closer look (till scam, pressure
    sales reversed, or a training gap)."""
    from django.db.models.functions import TruncDate

    from apps.pos.models import PosSale
    window = today - timedelta(days=30)
    voided = PosSale.objects.filter(store=store, status=PosSale.VOID, voided_at__date__gte=window)
    if not voided.exists():
        return Check("pos_voids", "Struk kasir dibatalkan tidak berulang", OK,
            "Tidak ada struk kasir dibatalkan dalam 30 hari terakhir.")
    by_day = list(voided.annotate(day=TruncDate("voided_at")).values("cashier__username", "day").annotate(
        count=Count("pk")).filter(count__gte=3).order_by("-count"))
    if not by_day:
        return Check("pos_voids", "Struk kasir dibatalkan tidak berulang", OK,
            f"{voided.count()} struk dibatalkan dalam 30 hari, tidak ada kasir yang berulang dalam sehari.")
    check = Check("pos_voids", "Struk kasir dibatalkan tidak berulang", WARN,
        f"{len(by_day)} kasir membatalkan 3 struk atau lebih dalam sehari. Periksa alasan pembatalannya.",
        url=reverse("merchant-pos"), action="Buka kasir")
    check.items = [f"{row['cashier__username']} · {row['day']:%d/%m} · {row['count']} struk" for row in by_day[:10]]
    return check


def pos_variance_trend(store, today, ledger):
    """A cashier whose drawer is short shift after shift is worth a closer look, not just the one
    whose shift is open overnight (``pos_shifts``)."""
    from apps.pos.models import PosShift
    window = today - timedelta(days=30)
    shifts = PosShift.objects.filter(register__store=store, status=PosShift.CLOSED, closed_on__gte=window,
        variance__lt=0).select_related("closed_by")
    by_cashier = {}
    for shift in shifts:
        name = shift.closed_by.get_username() if shift.closed_by else "?"
        by_cashier[name] = by_cashier.get(name, 0) + 1
    flagged = sorted(((name, count) for name, count in by_cashier.items() if count >= 3), key=lambda row: -row[1])
    if not flagged:
        return Check("pos_variance_trend", "Selisih kas kasir tidak berulang", OK,
            "Tidak ada kasir dengan laci kurang berulang dalam 30 hari terakhir.")
    check = Check("pos_variance_trend", "Selisih kas kasir tidak berulang", WARN,
        f"{len(flagged)} kasir menutup shift dengan laci kurang (selisih minus) 3 kali atau lebih dalam 30 hari.",
        url=reverse("merchant-pos"), action="Buka kasir")
    check.items = [f"{name} · {count} shift kurang" for name, count in flagged[:10]]
    return check


CHECKS = [trial_balance, unposted_documents, invoice_vs_journal, invoice_open_balance, invoice_paid_status, receivable, payable,
    inventory_value, negative_inventory, stock_movements, stock_changes_booked, cash_accounts, held_by_providers,
    customer_credit, unidentified, paid_orders_without_invoice, unmapped_skus, sold_without_cost,
    marketplace_income, period_lock, budget_control, duplicate_bills, unusual_expenses,
    large_manual_journals, backdated_entries, voids_after_payment, withholding_tie, year_close_tie, grni_tie,
    delivered_tie, landed_cost_tie, lots_tie, intercompany_tie, negative_stock, purchase_orders_late, pos_receipts, pos_shifts, pos_voids,
    pos_variance_trend, payroll_tie, marketplace_pph22_tie]


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
