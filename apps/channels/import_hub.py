"""Impor data: every import on one page, each with what it accepts, its template and what the last run did.

Nothing is imported here: each card opens the page that already checks a file and shows a preview before saving.
A card is shown only when the person may use that import and the company's plan includes its page."""
from django.urls import NoReverseMatch, reverse

from apps.shops.models import AuditLog
from apps.shops.permissions import can


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


CARDS = [
    # key, title, what it does, accepts, page, template, permission, group
    ("sales", "Penjualan dari Excel", ("Rekap penjualan harian (Tanggal, No. Pesanan, SKU, Penjualan, Penjualan Bersih, "
     "Ongkir) menjadi invoice: stok keluar, HPP, mitra, retur."), ("Template Patuhdata · rekap Excel Anda · faktur "
     "penjualan Accurate"), "merchant-invoice-batch", "merchant-invoice-batch-template", "sales.edit", "Penjualan"),
    ("marketplace", "Pesanan marketplace", ("Laporan pesanan dari Seller Center: pesanan selesai menjadi penjualan, yang "
     "batal ikut tercatat."), "Shopee · TikTok Shop / Tokopedia · Lazada", "merchant-import", "", "channels.manage",
     "Marketplace"),
    ("income", "Penghasilan marketplace", ("Laporan penghasilan / settlement: dana cair ke rekening, biaya per jenis, "
     "PPh 22."), "Shopee · TikTok Shop / Tokopedia · Lazada", "merchant-income-import", "", "channels.income",
     "Marketplace"),
    ("receipts", "Penerimaan pembayaran", ("Pembayaran pelanggan melunasi invoice (dicocokkan dengan nomor invoice atau "
     "Referensi)."), "Template Patuhdata · penerimaan penjualan Accurate", "merchant-receipt-import",
     "merchant-receipt-import-template", "sales.edit", "Penjualan"),
    ("products", "Produk", ("Produk, SKU, harga, harga pokok, dan stok. Kunci SKU atau nama produk; periksa dulu sebelum "
     "simpan."), "Template Patuhdata · daftar barang Accurate", "merchant-product-import",
     "merchant-product-import-template", "catalog.edit", "Stok"),
    ("customers", "Pelanggan", "Nama, kode pelanggan, kontak, alamat, NPWP, termin.", ("Template Patuhdata · pelanggan "
     "Accurate (isi ID Pelanggan sama dengan Customer No. di faktur)"), "merchant-customer-import",
     "merchant-customer-import-template", "customers.edit", "Pelanggan"),
    ("opening_ar", "Saldo awal piutang", ("Invoice lama yang belum dibayar saat mulai memakai Patuhdata: hanya "
     "piutangnya, tanpa penjualan, stok, atau PPN."), "Tempel dari Excel", "merchant-opening-invoices", "",
     "books.edit", "Saldo awal"),
    ("opening_ap", "Saldo awal utang", "Tagihan supplier lama yang belum dibayar: hanya utangnya.",
     "Tempel dari Excel", "merchant-opening-bills", "", "books.edit", "Saldo awal"),
]
LAST_ACTION = {"sales": ("invoice.created", {"details__mode": "batch"}), "income": ("channel.income_imported", {}),
    "receipts": ("receipts.imported", {}), "products": ("products.csv_imported", {}),
    "customers": ("customers.imported", {})}


def _last(store, key):
    """``(when, who, text)`` of the last run of this import, or None."""
    if key == "marketplace":
        from .models import ImportBatch
        batch = ImportBatch.objects.filter(store=store).select_related("channel", "imported_by").order_by(
            "-imported_at").first()
        if batch:
            return batch.imported_at, batch.imported_by, f"{batch.channel.name}: {batch.file_name or 'file'}"
        return None
    if key in ("opening_ar", "opening_ap"):
        if key == "opening_ar":
            from apps.tax.models import Invoice
            row = Invoice.objects.filter(store=store, order__order_type="opening").order_by("-created_at").first()
        else:
            from apps.accounting.models import Bill
            row = Bill.objects.filter(store=store, is_opening=True).order_by("-created_at").first()
        return (row.created_at, None, f"terakhir {row.number}") if row else None
    action, extra = LAST_ACTION[key]
    log = AuditLog.objects.filter(store=store, action=action, **extra).select_related("actor").order_by(
        "-created_at").first()
    if log is None:
        return None
    details = log.details or {}
    if key == "sales":
        text = "invoice dibuat dari Excel"
    elif key == "income":
        text = f"{details.get('orders', 0)} pesanan, {details.get('payouts', 0)} pencairan"
    elif key in ("products",):
        text = f"{details.get('created', 0)} baru, {details.get('updated', 0)} diperbarui"
    elif key == "customers":
        text = f"{details.get('created', details.get('rows', 0))} pelanggan"
    else:
        text = f"{details.get('count', details.get('receipts', ''))} penerimaan".strip()
    return log.created_at, log.actor, text


def cards(store, user):
    """The import cards this person may use, in the order a company moving in needs them."""
    from apps.billing.modules import full_books, path_open
    everything = full_books(store)
    out = []
    for key, title, what, accepts, page, template, permission, group in CARDS:
        url = _url(page)
        if not url or not can(user, store, permission) or (not everything and not path_open(url)):
            continue
        out.append({"key": key, "title": title, "what": what, "accepts": accepts, "url": url,
            "template": _url(template) if template else "", "group": group, "last": _last(store, key)})
    return out
