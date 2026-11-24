"""Excel workbooks: one per financial report, and the auditor's yearly pack.

Every number comes from the same service the screen uses (``reports``, ``payables``,
``receivables``...), never from a second query, so a workbook always agrees with the page it
was opened from. Cells hold real numbers with a rupiah format, not text."""
import datetime
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import reports
from .ledger import account_balances, chart_of_accounts
from .models import FixedAsset, JournalLine, Withholding

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RP = '#,##0;[Red]-#,##0'  # rupiah: whole numbers


def money_format():
    """Excel number format for the company's book currency (apps/shops/currency.py): no decimals for rupiah,
    two for dollars and euros."""
    from apps.shops import currency
    places = currency.current().decimals
    return RP if not places else f"#,##0.{'0' * places};[Red]-#,##0.{'0' * places}"
BLUE, SKY, ICE = "0066FF", "EBF3FF", "F4F8FF"
HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor=BLUE)
SECTION_FILL = PatternFill("solid", fgColor=SKY)
TOTAL_BORDER = Border(top=Side(style="thin", color=BLUE))


class Sheet:
    """A worksheet written top to bottom: title lines, then tables with a header row."""

    def __init__(self, wb, title, store=None, subtitle="", *, first=False):
        self.ws = wb.active if first else wb.create_sheet()
        self.ws.title = title[:31]
        self.row = 1
        self.widths = {}
        self.ws.sheet_view.showGridLines = False
        if store is not None:
            self.line(store.name, bold=True, size=14)
            self.line(f"{title}" + (f" · {subtitle}" if subtitle else ""), color="526685")
            self.row += 1

    def line(self, text, *, bold=False, size=11, color="0F1E36"):
        cell = self.ws.cell(self.row, 1, text)
        cell.font = Font(bold=bold, size=size, color=color)
        self.row += 1

    def header(self, *names):
        for col, name in enumerate(names, 1):
            cell = self.ws.cell(self.row, col, name)
            cell.font, cell.fill = HEAD, HEAD_FILL
            cell.alignment = Alignment(horizontal="left" if col == 1 else "center", vertical="center")
            self._fit(col, name)
        self.row += 1

    def section(self, title, width=2):
        for col in range(1, width + 1):
            self.ws.cell(self.row, col).fill = SECTION_FILL
        self.ws.cell(self.row, 1, title).font = Font(bold=True, color=BLUE)
        self.row += 1

    def add(self, *values, bold=False, total=False, fmt=None):
        for col, value in enumerate(values, 1):
            if isinstance(value, Decimal):
                value = float(value)
            cell = self.ws.cell(self.row, col, value)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cell.number_format = fmt or money_format()
                cell.alignment = Alignment(horizontal="right")
            elif isinstance(value, (datetime.date, datetime.datetime)):
                cell.number_format = "dd/mm/yyyy"
                cell.alignment = Alignment(horizontal="left")
            if bold or total:
                cell.font = Font(bold=True)
            if total:
                cell.border = TOTAL_BORDER
            self._fit(col, value)
        self.row += 1

    def blank(self):
        self.row += 1

    def _fit(self, col, value):
        size = len(f"{value:,.0f}") if isinstance(value, (int, float, Decimal)) else len(str(value or ""))
        self.widths[col] = min(max(self.widths.get(col, 8), size + 3), 60)

    def finish(self, freeze=None):
        for col, width in self.widths.items():
            self.ws.column_dimensions[get_column_letter(col)].width = width
        if freeze:
            self.ws.freeze_panes = freeze


def response(wb, filename):
    buffer = BytesIO()
    wb.save(buffer)
    out = HttpResponse(buffer.getvalue(), content_type=XLSX)
    out["Content-Disposition"] = f'attachment; filename="{filename}"'
    return out


# ---------------------------------------------------------------------------
# The five reports
# ---------------------------------------------------------------------------

def profit_loss(wb, store, start, end, *, first=False):
    report = reports.profit_and_loss(store, start, end)
    sheet = Sheet(wb, "Laba rugi", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}", first=first)
    sheet.header("Kode", "Akun", "Periode ini", "Periode sebelumnya")
    totals = report["totals"]
    for key, title in [("revenue", "Pendapatan"), ("cogs", "Harga pokok penjualan"),
            ("operating", "Beban operasional"), ("other_income", "Pendapatan lain"),
            ("other_expense", "Beban lain")]:
        sheet.section(title, 4)
        for row in report["sections"][key]:
            sheet.add(row["code"], row["name"], row["amount"], row["previous"])
        sheet.add("", f"Jumlah {title.lower()}", totals[key]["amount"], totals[key]["previous"], total=True)
        if key == "cogs":
            sheet.add("", "Laba kotor", totals["gross"]["amount"], totals["gross"]["previous"], bold=True)
    sheet.blank()
    sheet.add("", "Laba bersih", totals["net"]["amount"], totals["net"]["previous"], bold=True, total=True)
    sheet.finish("A5")
    return sheet


def balance_sheet(wb, store, as_of, *, first=False):
    compare = reports._shift_months(as_of, -1)
    sheet_data = reports.balance_sheet(store, as_of, compare_to=compare)
    sheet = Sheet(wb, "Neraca", store, f"per {as_of:%d/%m/%Y}", first=first)
    sheet.header("Kode", "Akun", f"{as_of:%d/%m/%Y}", f"{compare:%d/%m/%Y}")
    totals = sheet_data["totals"]
    for key, title in [("current_assets", "Aset lancar"), ("fixed_assets", "Aset tetap")]:
        sheet.section(title, 4)
        for row in sheet_data["sections"][key]:
            sheet.add(row["code"], row["name"], row["amount"], row["previous"])
    sheet.add("", "Total aset", totals["assets"]["amount"], totals["assets"]["previous"], total=True)
    sheet.blank()
    for key, title in [("current_liabilities", "Liabilitas jangka pendek"),
            ("long_liabilities", "Liabilitas jangka panjang"), ("equity", "Ekuitas")]:
        sheet.section(title, 4)
        for row in sheet_data["sections"][key]:
            sheet.add(row["code"], row["name"], row["amount"], row["previous"])
    sheet.add("", "Total liabilitas dan ekuitas", totals["liabilities_equity"]["amount"],
        totals["liabilities_equity"]["previous"], total=True)
    sheet.blank()
    sheet.add("", "Selisih (harus nol)", sheet_data["difference"])
    sheet.finish("A5")
    return sheet


def cash_flow(wb, store, start, end, *, first=False):
    flow = reports.cash_flow(store, start, end)
    sheet = Sheet(wb, "Arus kas", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}", first=first)
    sheet.header("Kode", "Keterangan", "Jumlah")
    sheet.add("", "Saldo kas awal", flow["opening"], bold=True)
    for section in flow["sections"]:
        sheet.section(section["label"], 3)
        for row in section["rows"]:
            sheet.add(row["code"], row["name"], row["amount"])
        sheet.add("", f"Kas bersih {section['label'].lower()}", section["net"], total=True)
    sheet.blank()
    sheet.add("", "Kenaikan (penurunan) kas", flow["net"], bold=True)
    sheet.add("", "Saldo kas akhir", flow["closing"], bold=True, total=True)
    sheet.finish("A5")
    return sheet


def trial_balance(wb, store, *, through=None, first=False):
    chart_of_accounts(store)
    accounts = account_balances(store, end=through)
    sheet = Sheet(wb, "Neraca saldo", store, f"per {through:%d/%m/%Y}" if through else "seluruh periode",
        first=first)
    sheet.header("Kode", "Akun", "Tipe", "Debit", "Kredit", "Saldo")
    for account in accounts:
        if account.total_debit or account.total_credit:
            sheet.add(account.code, account.name, account.get_type_display(), account.total_debit,
                account.total_credit, account.balance)
    sheet.add("", "Total", "", sum((a.total_debit for a in accounts), Decimal(0)),
        sum((a.total_credit for a in accounts), Decimal(0)), "", total=True)
    sheet.finish("A5")
    return sheet


def general_ledger(wb, store, start, end, *, first=False, limit=None):
    """Every account with postings in the period: opening balance, lines, closing balance."""
    sheet = Sheet(wb, "Buku besar", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}", first=first)
    sheet.header("Tanggal", "No. jurnal", "Keterangan", "Debit", "Kredit", "Saldo")
    for account in sorted(chart_of_accounts(store).values(), key=lambda a: a.code):
        ledger = reports.account_ledger(account, start=start, end=end, limit=limit or 100000)
        if not ledger["lines"] and not ledger["opening"]:
            continue
        sheet.section(f"{account.code} · {account.name}", 6)
        sheet.add("", "", "Saldo awal", "", "", ledger["opening"], bold=True)
        for line in ledger["lines"]:
            sheet.add(line.entry.date, line.entry_id, line.entry.memo, line.debit, line.credit, line.running)
        sheet.add("", "", "Saldo akhir", ledger["debit"], ledger["credit"], ledger["closing"], total=True)
    sheet.finish("A5")
    return sheet


REPORTS = {
    "laba-rugi": ("Laba rugi", lambda wb, store, start, end: profit_loss(wb, store, start, end, first=True)),
    "neraca": ("Neraca", lambda wb, store, start, end: balance_sheet(wb, store, end, first=True)),
    "arus-kas": ("Arus kas", lambda wb, store, start, end: cash_flow(wb, store, start, end, first=True)),
    "neraca-saldo": ("Neraca saldo", lambda wb, store, start, end: trial_balance(wb, store, through=end, first=True)),
    "buku-besar": ("Buku besar", lambda wb, store, start, end: general_ledger(wb, store, start, end, first=True)),
    "mutasi-persediaan": ("Mutasi persediaan", lambda wb, store, start, end: inventory_mutation(wb, store, start, end,
        first=True)),
}


def inventory_mutation(wb, store, start, end, *, warehouse=None, first=False):
    """Mutasi persediaan (``inventory_reports.mutation``): units per item, then Persediaan in rupiah per document."""
    from .inventory_reports import MUTATION_COLUMNS, mutation
    report = mutation(store, start, end, warehouse=warehouse)
    where = f" · {warehouse.name}" if warehouse else ""
    sheet = Sheet(wb, "Mutasi unit", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}{where}", first=first)
    sheet.header("SKU", "Barang", "Saldo awal", *[label for _c, label, _k in MUTATION_COLUMNS], "Saldo akhir")
    for row in report["rows"]:
        sheet.add(row["sku"], row["name"], row["opening"], *[row[c] for c, _l, _k in MUTATION_COLUMNS], row["closing"],
            fmt="#,##0;[Red]-#,##0")
    units = report["units"]
    sheet.add("", "Jumlah", units["opening"], *[units[c] for c, _l, _k in MUTATION_COLUMNS], units["closing"],
        fmt="#,##0;[Red]-#,##0", total=True)
    sheet.finish("C5")
    value = report["value"]
    money = Sheet(wb, "Mutasi nilai", store, f"Persediaan barang (1400) · {start:%d/%m/%Y} s.d. {end:%d/%m/%Y}")
    money.header("Dokumen", "Masuk (debit)", "Keluar (kredit)")
    money.add("Saldo awal", value["opening"], "", bold=True)
    for row in value["rows"]:
        money.add(row["label"], row["debit"], row["credit"])
    money.add("Jumlah mutasi", value["debit"], value["credit"], total=True)
    money.add("Saldo akhir", value["closing"], "", bold=True)
    money.finish("A5")
    return sheet


def report_workbook(kind, store, start, end):
    wb = Workbook()
    REPORTS[kind][1](wb, store, start, end)
    return wb


# ---------------------------------------------------------------------------
# Paket audit tahunan
# ---------------------------------------------------------------------------

def audit_workbook(store, year, *, with_activity=True):
    """What an auditor asks for after year end, one sheet each, all as of 31 December."""
    from apps.shops.models import AuditLog
    from apps.tax import spt
    from apps.tax.models import Invoice
    from apps.tax.receivables import BUCKETS as AR_BUCKETS
    from apps.tax.receivables import aging as ar_aging

    from . import assets as asset_service
    from . import inventory
    from .payables import AGING_BUCKETS
    from .payables import aging as ap_aging

    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    wb = Workbook()
    ws = Sheet(wb, "Ringkasan", store, f"Paket audit tahun {year}", first=True)
    ws.add("Periode", f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}")
    ws.add("Dibuat", datetime.date.today())
    ws.blank()
    ws.header("Lembar", "Isi")
    for name, text in [("Neraca saldo", "Saldo semua akun per 31 Desember"),
            ("Laba rugi", "Laporan laba rugi tahun ini dan tahun sebelumnya"),
            ("Neraca", "Posisi keuangan per 31 Desember"), ("Jurnal", "Setiap baris jurnal tahun ini"),
            ("Piutang", "Umur piutang per pelanggan"), ("Utang", "Umur utang per supplier"),
            ("Aset tetap", "Daftar aset, akumulasi penyusutan, nilai buku"),
            ("Persediaan", "Stok dinilai pada harga pokok"), ("Penjualan", "Invoice penjualan tahun ini"),
            ("PPN", "PPN keluaran, masukan, dan kompensasi per bulan"),
            ("PPh", "PPh dipotong dan status setoran"), ("Log aktivitas", "Siapa mengubah apa (opsional)")]:
        ws.add(name, text)
    ws.finish()

    trial_balance(wb, store, through=end)
    profit_loss(wb, store, start, end)
    balance_sheet(wb, store, end)

    journal = Sheet(wb, "Jurnal", store, f"tahun {year}")
    journal.header("Tanggal", "No. jurnal", "Sumber", "Keterangan", "Kode akun", "Akun", "Debit", "Kredit",
        "Dibuat oleh", "Dibatalkan")
    lines = (JournalLine.objects.filter(entry__store=store, entry__date__gte=start, entry__date__lte=end)
        .select_related("entry", "account", "entry__created_by").order_by("entry__date", "entry_id", "id"))
    for line in lines.iterator():
        entry = line.entry
        journal.add(entry.date, entry.pk, f"{entry.source_type}:{entry.source_id}"[:40], entry.memo,
            line.account.code, line.account.name, line.debit, line.credit,
            entry.created_by.username if entry.created_by else "sistem", "ya" if entry.superseded else "")
    journal.finish("A5")

    rows, totals = ar_aging(store, end)
    receivable = Sheet(wb, "Piutang", store, f"umur per {end:%d/%m/%Y}")
    receivable.header("Pelanggan", *[label for _, label in AR_BUCKETS], "Total")
    for row in rows:
        receivable.add(row["name"], *[row[key] for key, _ in AR_BUCKETS], row["total"])
    receivable.add("Total", *[totals[key] for key, _ in AR_BUCKETS], totals["total"], total=True)
    receivable.finish("A5")

    rows, totals = ap_aging(store, end)
    payable = Sheet(wb, "Utang", store, f"umur per {end:%d/%m/%Y}")
    payable.header("Supplier", *[label for _, label in AGING_BUCKETS], "Total")
    for row in rows:
        payable.add(row["name"], *[row[key] for key, _ in AGING_BUCKETS], row["total"])
    payable.add("Total", *[totals[key] for key, _ in AGING_BUCKETS], totals["total"], total=True)
    payable.finish("A5")

    fixed = Sheet(wb, "Aset tetap", store, f"per {end:%d/%m/%Y}")
    fixed.header("Nomor", "Aset", "Diperoleh", "Harga perolehan", "Umur (bulan)", "Akumulasi penyusutan",
        "Nilai buku", "Status")
    for asset in FixedAsset.objects.filter(store=store, acquired_on__lte=end).order_by("acquired_on"):
        accumulated = asset_service.accumulated(asset, through=end)
        fixed.add(asset.number, asset.name, asset.acquired_on, asset.cost, asset.useful_life_months,
            accumulated, asset.cost - accumulated, asset.get_status_display())
    fixed.finish("A5")

    stock = inventory.stock_valuation(store)
    shelf = Sheet(wb, "Persediaan", store, "nilai stok saat ini (bukan per 31 Desember)")
    shelf.header("SKU", "Barang", "Di gudang", "Di mitra", "Harga pokok", "Nilai gudang", "Nilai mitra")
    for row in stock["rows"]:
        shelf.add(row["sku"], row["name"], row["shelf"], row["mitra"], row["cost"], row["shelf_value"],
            row["mitra_value"])
    shelf.add("", "Total", "", "", "", stock["shelf_value"], stock["mitra_value"], total=True)
    shelf.finish("A5")

    sales = Sheet(wb, "Penjualan", store, f"invoice tahun {year}")
    sales.header("Nomor", "Tanggal", "Pembeli", "NPWP pembeli", "DPP", "PPN", "Total", "Status")
    for invoice in Invoice.objects.filter(store=store, issued_on__gte=start, issued_on__lte=end
            ).exclude(status=Invoice.STATUS_DRAFT).order_by("issued_on", "number"):
        sales.add(invoice.number, invoice.issued_on, invoice.buyer_name, invoice.buyer_npwp,
            invoice.tax_base, invoice.tax, invoice.total, invoice.get_status_display())
    sales.finish("A5")

    vat = Sheet(wb, "PPN", store, f"tahun {year}")
    vat.header("Bulan", "PPN keluaran", "PPN masukan", "Kompensasi masuk", "Kurang bayar", "Lebih bayar")
    for month in range(1, 13):
        w = spt.worksheet(store, year, month)
        vat.add(f"{year}-{month:02d}", w["out"], w["in"], w["carry_in"], w["to_pay"], w["overpaid"])
    vat.finish("A5")

    pph = Sheet(wb, "PPh", store, f"tahun {year}")
    pph.header("No. bukti potong", "Tanggal", "Jenis", "Penerima", "NPWP", "DPP", "Tarif %", "PPh", "Disetor")
    for w in Withholding.objects.filter(store=store, date__gte=start, date__lte=end).select_related("deposit"):
        pph.add(w.number, w.date, f"{w.tax_name} {w.get_kind_display()}", w.payee_name, w.payee_npwp, w.base,
            w.rate, w.amount, "ya" if w.deposit_id else "belum")
    pph.finish("A5")

    if with_activity:
        log = Sheet(wb, "Log aktivitas", store, f"tahun {year}")
        log.header("Waktu", "Pengguna", "Tindakan", "Objek", "ID", "Rincian")
        for item in AuditLog.objects.filter(store=store, created_at__date__gte=start,
                created_at__date__lte=end).select_related("actor").order_by("created_at")[:50000]:
            log.add(item.created_at.replace(tzinfo=None), item.actor.username if item.actor else "sistem",
                item.action, item.object_type, item.object_id, str(item.details)[:200])
        log.finish("A5")
    return wb


# ---------------------------------------------------------------------------
# Laporan kuartal
# ---------------------------------------------------------------------------

def quarter_workbook(store, year, quarter):
    """The quarter's statements, one sheet each, from the same services as the screen."""
    start, end = reports.quarter_bounds(year, quarter)
    wb = Workbook()
    table = reports.quarterly_profit(store, year)
    sheet = Sheet(wb, "Per kuartal", store, f"tahun {year}", first=True)
    sheet.header("Keterangan", "Kuartal 1", "Kuartal 2", "Kuartal 3", "Kuartal 4", "Tahun")
    for key, label in (("revenue", "Pendapatan"), ("cogs", "Harga pokok penjualan"), ("gross", "Laba kotor"),
            ("operating", "Beban operasional"), ("other", "Pendapatan (beban) lain"), ("net", "Laba bersih")):
        sheet.add(label, *[q[key] for q in table["quarters"]], table["total"][key], bold=key in ("gross", "net"))
    sheet.add("Margin kotor %", *[q["gross_margin"] if q["gross_margin"] is not None else "" for q in table["quarters"]], "", fmt="0.0")
    sheet.add("Margin bersih %", *[q["net_margin"] if q["net_margin"] is not None else "" for q in table["quarters"]], "", fmt="0.0")
    sheet.finish("A5")
    profit_loss(wb, store, start, end)
    balance_sheet(wb, store, end)
    flow = reports.cash_flow_indirect(store, start, end)
    cf = Sheet(wb, "Arus kas (tidak langsung)", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}")
    cf.header("Keterangan", "Jumlah")
    for section in flow["sections"]:
        cf.section(section["label"], 2)
        for label, value in section["rows"]:
            cf.add(label, value)
        cf.add(f"Kas bersih {section['label'].lower()}", section["net"], total=True)
    cf.blank()
    cf.add("Kenaikan (penurunan) kas", flow["net"], bold=True)
    cf.add("Kas awal", flow["opening"])
    cf.add("Kas akhir", flow["closing"], bold=True, total=True)
    cf.add("Selisih (harus nol)", flow["difference"])
    cf.finish("A5")
    eq = reports.equity_statement(store, start, end)
    es = Sheet(wb, "Perubahan ekuitas", store, f"{start:%d/%m/%Y} s.d. {end:%d/%m/%Y}")
    es.header("Keterangan", "Jumlah")
    for label, value in eq["rows"]:
        es.add(label, value)
    es.add("Ekuitas akhir", eq["closing"], bold=True, total=True)
    es.finish("A5")
    return wb


# ---------------------------------------------------------------------------
# Paket pajak marketplace (one month, for the accountant)
# ---------------------------------------------------------------------------

def marketplace_tax_workbook(store, year, month):
    """The month's marketplace selling, from ``channels.marketplace_tax.build`` (the same service as the screen):
    a summary per shop, every order, and what still needs fixing."""
    from apps.channels import marketplace_tax
    from apps.tax.services import month_bounds, monthly_tax, tax_profile
    start, end = month_bounds(year, month)
    pack = marketplace_tax.build(store, start, end)
    tax, profile = monthly_tax(store, year, month), tax_profile(store)
    wb = Workbook()
    period = f"{start:%m/%Y}"
    sheet = Sheet(wb, "Ringkasan", store, f"Paket pajak marketplace {period}", first=True)
    shops = pack["shops"]
    names = [f"{r['channel'].get_platform_display()} · {r['channel'].name}" for r in shops]
    sheet.header("Penjualan (tanggal invoice)", *names, "Jumlah")

    def line(label, key, *, total=False):
        sheet.add(label, *[r[key] for r in shops], pack["totals"][key], total=total)

    line("Jumlah invoice", "invoices")
    line("DPP", "dpp")
    line("PPN keluaran", "vat")
    line("Total ditagih", "total", total=True)
    line("Retur / nota kredit", "returns")
    line("PPN atas retur", "returns_vat")
    line("Penjualan bersih", "net_sales", total=True)
    sheet.blank()
    sheet.header("Pencairan (tanggal dana dilepas)", *names, "Jumlah")
    line("Pesanan dicairkan", "released_count")
    line("Nilai pesanan dicairkan", "gross_released")
    for name, label in pack["fee_labels"]:
        sheet.add(label, *[r["fees"][name] for r in shops], pack["totals"]["fees"][name])
    line("Jumlah biaya marketplace", "fees_total", total=True)
    line("PPh 22 dipungut marketplace", "pph22")
    line("Dana masuk ke bank", "net", total=True)
    sheet.blank()
    line("Belum dicairkan s.d. akhir bulan (piutang marketplace)", "awaiting_value")
    sheet.blank()
    sheet.section("Pajak bulan ini (seluruh perusahaan, dari buku besar)", len(names) + 2)
    sheet.add("Omzet bruto", tax.turnover)
    sheet.add("PPN keluaran", tax.vat)
    if profile.pph_regime == "umkm_final":
        sheet.add("PPh Final UMKM 0,5%", tax.pph)
        sheet.add("Sudah dipungut marketplace (PPh 22)", tax.marketplace_pph22)
        sheet.add("Sisa PPh Final disetor", tax.pph_to_deposit, total=True)
    else:
        sheet.add("PPh 22 dipungut marketplace (kredit pajak SPT Tahunan, akun 1525)", tax.marketplace_pph22)
    if pack["checks"]:
        sheet.blank()
        sheet.section("Perlu dicek sebelum diserahkan", len(names) + 2)
        for _level, text, _url in pack["checks"]:
            sheet.add(text)
    sheet.finish("A5")

    orders = Sheet(wb, "Per pesanan", store, period)
    fee_names = [name for name, _label in pack["fee_labels"]]
    orders.header("Tanggal invoice", "Marketplace", "Toko", "No. pesanan", "No. invoice", "Pembeli", "DPP", "PPN",
        "Total", "Retur", *[label for _name, label in pack["fee_labels"]], "PPh 22", "Dana cair", "Tanggal cair",
        "Referensi pencairan")
    for row in pack["orders"]:
        orders.add(row["date"], row["platform"], row["shop"], row["order"], row["invoice"].number, row["buyer"],
            row["dpp"], row["vat"], row["total"], row["returned"], *[row["fees"][n] for n in fee_names], row["pph22"],
            row["net"], row["released_on"], row["payout"])
    orders.add("Jumlah", "", "", "", "", "", pack["totals"]["dpp"], pack["totals"]["vat"], pack["totals"]["total"],
        pack["totals"]["returns"], total=True)
    orders.finish("A5")
    return wb
