"""Laporan keuangan SAK EMKM: Laporan Posisi Keuangan, Laporan Laba Rugi and Catatan atas
Laporan Keuangan for a year, built from the same balance sheet and profit and loss the screens
show, so the pack cannot disagree with the books."""
import datetime
from decimal import Decimal

from . import reports
from .models import FixedAsset

ZERO = Decimal(0)

POLICIES = [
    ("Dasar penyusunan", "Laporan keuangan disusun berdasarkan Standar Akuntansi Keuangan Entitas Mikro, "
        "Kecil, dan Menengah (SAK EMKM) dengan dasar akrual, dalam mata uang Rupiah."),
    ("Pengakuan pendapatan", "Pendapatan penjualan diakui saat invoice diterbitkan. Retur, refund, dan "
        "potongan penjualan dicatat sebagai pengurang pendapatan."),
    ("Persediaan", "Persediaan diukur pada biaya perolehan dengan metode rata-rata bergerak. Harga pokok "
        "penjualan dicatat pada saat barang keluar."),
    ("Aset tetap", "Aset tetap dicatat pada harga perolehan dikurangi akumulasi penyusutan. Penyusutan "
        "dihitung dengan metode garis lurus selama umur manfaat aset."),
    ("Pajak", "PPN dicatat terpisah dari pendapatan dan biaya bagi Pengusaha Kena Pajak. PPh yang dipotong "
        "dicatat sebagai utang sampai disetor."),
]


def _sum(rows):
    return sum((row["amount"] for row in rows), ZERO)


def pack(store, year):
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    pl = reports.profit_and_loss(store, start, end, compare=False)
    prev = reports.profit_and_loss(store, datetime.date(year - 1, 1, 1), datetime.date(year - 1, 12, 31),
        compare=False)
    sheet = reports.balance_sheet(store, end, compare_to=datetime.date(year - 1, 12, 31))
    totals = pl["totals"]
    profile = getattr(store, "tax_profile", None)
    assets = list(FixedAsset.objects.filter(store=store, acquired_on__lte=end).order_by("acquired_on"))
    from . import assets as asset_service
    fixed = []
    for a in assets:
        accumulated = asset_service.accumulated(a, through=end)
        fixed.append({"name": a.name, "cost": a.cost, "accumulated": accumulated, "book": a.cost - accumulated})
    return {
        "year": year, "start": start, "end": end, "store": store,
        "entity": (profile.legal_name if profile and profile.legal_name else store.name),
        "npwp": profile.npwp if profile else "", "address": profile.address if profile else "",
        "pl": pl, "prev": prev, "sheet": sheet,
        "net_sales": totals["revenue"]["amount"], "net_profit": totals["net"]["amount"],
        "prev_net_profit": prev["totals"]["net"]["amount"],
        "fixed": fixed, "fixed_cost": sum((f["cost"] for f in fixed), ZERO),
        "fixed_accumulated": sum((f["accumulated"] for f in fixed), ZERO),
        "fixed_book": sum((f["book"] for f in fixed), ZERO),
        "policies": POLICIES, "balanced": sheet["balanced"],
    }
