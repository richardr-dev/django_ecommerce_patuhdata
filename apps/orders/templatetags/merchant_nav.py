"""The back-office sidebar, defined once.

Sections follow an accounting product (Jurnal, Accurate, Xero): sales, purchasing, cash and bank,
inventory, accounting (ledger, closing, reports, tax), then the sales channels (marketplaces, mitra and
the online shop, which is one optional channel of the company), customers and settings.
Each link knows which paths belong to it; the most specific match is the active page and its
group opens by itself, so the sidebar always shows where you are."""
from dataclasses import dataclass, field

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


@dataclass
class Link:
    label: str
    url: str
    args: tuple = ()
    query: str = ""  # e.g. "channel=abshop": active only with this query
    match: tuple = ()  # extra path prefixes; "=/path/" matches that path exactly
    roles: frozenset = frozenset()  # empty: everyone
    when: str = ""  # context flag that must be truthy
    badge: str = ""  # context key holding a count


@dataclass
class Group:
    label: str
    icon: str
    links: list = field(default_factory=list)
    when: str = ""  # context flag that must be truthy for the whole group


ADMIN = frozenset({"admin"})

NAV = [
    ("Penjualan", [
        Group("Penjualan", "receipt", [
            Link("Ringkasan penjualan", "merchant-sales", match=("=/books/sales/",
                "/books/sales/stats/")),
            Link("Analitik penjualan", "merchant-sales-analytics"),
            Link("Pesanan", "merchant-orders", match=("/books/orders/",)),
            # Creating an invoice is one button on this page (and in "Buat baru"), not a menu item.
            Link("Invoice penjualan", "merchant-sales-invoices",
                match=("/books/sales/invoices/",)),
            Link("Penawaran", "merchant-quotes"),
            Link("Pesanan penjualan", "merchant-sales-orders", match=("/books/sales/orders/",)),
            Link("Surat jalan", "merchant-deliveries", match=("/books/sales/deliveries/",)),
            Link("Kasir (POS)", "merchant-pos", match=("/books/pos/",)),
            Link("Daftar harga", "merchant-price-lists", match=("/books/sales/price-lists/",)),
            Link("Retur & penukaran", "merchant-returns"),
            Link("Rekap produksi", "merchant-production-recap"),
        ]),
        Group("Piutang & pembayaran", "cash", [
            Link("Piutang pelanggan", "merchant-receivables"),
            Link("Terima pembayaran", "merchant-receive-payment",
                match=("/books/sales/receipts/",)),
            Link("Kredit pelanggan", "merchant-customer-credit"),
            Link("Nota kredit", "merchant-credit-notes"),
            Link("Invoice berulang", "merchant-recurring"),
        ]),
    ]),
    ("Pembelian", [
        Group("Pembelian", "truck", [
            Link("Ringkasan pembelian", "merchant-purchasing"),
            Link("Usulan pembelian", "merchant-reorder"),
            Link("Pesanan pembelian", "merchant-purchase-orders", match=("/books/accounting/pesanan-pembelian/",)),
            Link("Penerimaan barang", "merchant-goods-receipts", match=("/books/accounting/penerimaan/",)),
            Link("Tagihan supplier", "merchant-bills", match=("/books/accounting/bills/",)),
            Link("Beli tunai", "merchant-cash-purchase"),
            Link("Nota debit", "merchant-debit-notes", match=("/books/accounting/nota-debit/",)),
            Link("Biaya perolehan", "merchant-landed-costs", match=("/books/accounting/biaya-perolehan/",)),
            Link("Supplier", "merchant-suppliers", match=("/books/accounting/suppliers/",)),
            Link("Umur utang", "merchant-payables"),
            Link("Transfer antar brand", "merchant-intercompany", match=("/books/accounting/antar-brand/",)),
        ]),
    ]),
    ("Kas & bank", [
        Group("Kas & bank", "bank", [
            Link("Kas & bank", "merchant-bank-accounts", match=("/books/accounting/cash/",)),
            Link("Biaya operasional", "merchant-expenses"),
            Link("Pencairan dana", "merchant-payouts"),
            Link("Pinjaman", "merchant-loans", match=("/books/accounting/pinjaman/",)),
            Link("Penggajian", "merchant-payroll", match=("/books/penggajian/",)),
            Link("Saldo pembayaran Patuhdata", "merchant-platform-balance", when="uses_platform_gateway"),
        ]),
    ]),
    ("Persediaan", [
        Group("Stok & Produk", "box", [
            Link("Semua produk", "merchant-entity-list", ("products",), match=("/books/products/",)),
            Link("Kategori", "merchant-entity-list", ("categories",)),
            Link("Paket (bundle)", "merchant-bundles"),
            Link("Satuan barang", "merchant-units"),
            Link("Lot & kedaluwarsa", "merchant-lots", match=("/books/stock/lot/",)),
            Link("Stok gudang", "merchant-stock"),
            Link("Stok opname", "merchant-stock-counts", match=("/books/accounting/stok-opname/",)),
            Link("Penyesuaian stok", "merchant-stock-adjustments", match=("/books/accounting/penyesuaian-stok/",)),
            Link("Permintaan stok", "merchant-stock-requests", match=("/books/stock/permintaan/",)),
            Link("Stok minus", "merchant-stock-minus"),
            Link("Gudang & outlet", "merchant-entity-list", ("warehouses",)),
            Link("Lokasi stok", "merchant-entity-list", ("inventory",)),
            Link("Impor produk", "merchant-product-import"),
            Link("Foto produk", "merchant-entity-list", ("images",)),
        ]),
        Group("Laporan persediaan", "layers", [
            Link("Nilai persediaan", "merchant-inventory-value"),
            Link("Kartu stok", "merchant-stock-card"),
            Link("Mutasi persediaan", "merchant-inventory-mutation"),
            Link("Stok lambat bergerak", "merchant-slow-moving"),
            Link("Penutupan persediaan", "merchant-inventory-close"),
        ]),
    ]),
    ("Akuntansi", [
        Group("Buku besar", "book", [
            Link("Ringkasan", "merchant-accounting", match=("=/books/accounting/",)),
            Link("Jurnal", "merchant-journal-list"),
            Link("Jurnal umum", "merchant-journal-new"),
            Link("Jurnal penyesuaian", "merchant-adjustments"),
            Link("Bagan akun", "merchant-chart-of-accounts"),
            Link("Saldo awal", "merchant-opening-balances"),
            Link("Aset tetap", "merchant-assets"),
            Link("Anggaran", "merchant-budget", match=("/books/accounting/budget/",)),
            Link("Pusat biaya", "merchant-cost-centers"),
        ]),
        Group("Tutup buku & kontrol", "lock", [
            Link("Pemeriksaan pembukuan", "merchant-books-checks"),
            Link("Tutup buku", "merchant-year-close"),
            Link("Persetujuan", "merchant-approvals", badge="approvals_pending"),
        ]),
        Group("Laporan Keuangan", "bar-chart", [
            Link("Laba rugi", "merchant-profit-loss"),
            Link("Neraca", "merchant-balance-sheet"),
            Link("Arus kas", "merchant-cash-flow"),
            Link("Neraca saldo", "merchant-trial-balance"),
            Link("Buku besar & neraca saldo", "merchant-finance"),
            Link("Laporan kuartal", "merchant-quarter-report"),
            Link("Rekap invoice per channel", "merchant-channel-recap"),
            Link("Ekspor data penjualan", "merchant-sales-export"),
            Link("Penjualan semua channel", "merchant-sales-overview"),
            Link("Penjualan bulanan", "merchant-sales-report"),
        ]),
        Group("Pajak", "percent", [
            Link("PPN & PPh", "merchant-tax"),
            Link("Kalender pajak", "merchant-tax-calendar"),
            Link("SPT Masa PPN", "merchant-spt-ppn"),
            Link("Pajak marketplace", "merchant-marketplace-tax"),
            Link("SPT Tahunan badan", "merchant-spt-tahunan"),
            Link("PPh dipotong", "merchant-withholdings", match=("/books/tax/pph/",)),
            Link("Tampilan invoice", "merchant-invoice-style", roles=frozenset({"admin", "manager", "finance"})),
        ]),
    ]),
    # Where the company sells. The online shop is one channel among these, not the product itself.
    ("Saluran penjualan", [
        Group("Marketplace", "link", [
            Link("Toko marketplace", "merchant-channels"),
            Link("Impor pesanan", "merchant-import"),
            Link("Penghasilan & biaya", "merchant-income-import"),
            Link("Pemetaan SKU", "merchant-sku-map"),
            Link("Stok per channel", "merchant-channel-stock"),
            Link("Pembeli marketplace", "merchant-channel-buyers"),
        ]),
        Group("Mitra konsinyasi", "handshake", [
            Link("Daftar mitra", "settlement-partners"),
            Link("Stok di mitra", "partner-stock"),
            Link("Pesanan mitra", "merchant-abshop"),
            Link("Invoice mitra", "merchant-sales-invoices", query="channel=abshop"),
            Link("Settlement", "settlement-list"),
        ]),
        Group("Toko online", "store", when="storefront", links=[
            Link("Status & alamat toko", "merchant-online-store"),
            Link("Tampilan toko", "merchant-settings"),
            Link("Atur banner", "merchant-entity-list", ("slides",)),
            Link("Halaman brand", "merchant-entity-list", ("pages",)),
            Link("Jadwal & pre-order", "merchant-delivery"),
            Link("Pelacakan & pixel", "merchant-tracking"),
            Link("Keranjang tertinggal", "merchant-engagement"),
            Link("Ulasan", "merchant-entity-list", ("reviews",)),
        ]),
        Group("Promosi", "megaphone", when="storefront", links=[
            Link("Flash sale & popup", "merchant-entity-list", ("campaigns",)),
            Link("Produk flash sale", "merchant-entity-list", ("flash-sales",)),
            Link("Voucher", "merchant-entity-list", ("coupons",)),
            Link("Gift card", "merchant-gift-cards"),
            Link("Link kampanye", "merchant-campaign-links"),
            Link("Feed & QR katalog", "merchant-feeds"),
        ]),
    ]),
    ("Pelanggan", [
        Group("Pelanggan", "users", [
            Link("Semua pelanggan", "merchant-customers", badge="pending_customers_count"),
            Link("Broadcast", "merchant-broadcasts"),
        ]),
    ]),
    ("", [
        Group("Pengaturan", "sliders", [
            Link("Pembayaran", "merchant-payment-settings"),
            Link("Mata uang", "merchant-currency-settings", roles=ADMIN),
            # Shipping methods serve manual orders and courier booking too, not only the online shop.
            Link("Pengiriman", "merchant-entity-list", ("shipping",), match=("/books/shipping-areas/",)),
            Link("WhatsApp", "merchant-whatsapp", roles=ADMIN),
            Link("Integrasi", "merchant-integrations"),
            Link("Staf & akses", "merchant-staff", roles=ADMIN, match=("/books/staff/",)),
            Link("Hak akses", "merchant-access", roles=ADMIN),
            Link("Log aktivitas", "merchant-activity", roles=ADMIN),
            Link("Paket langganan", "merchant-billing"),
        ]),
    ]),
]

# "Menu ringkas" (the default sidebar): the pages a small company uses every week. Everything else is one
# click away under "Menu lengkap", stays in the menu search, and shows anyway while it is the page you are on.
CORE = {
    "Penjualan": {"Ringkasan penjualan", "Pesanan", "Invoice penjualan", "Retur & penukaran"},
    "Piutang & pembayaran": {"Piutang pelanggan", "Terima pembayaran"},
    "Pembelian": {"Tagihan supplier", "Beli tunai", "Supplier"},
    "Kas & bank": {"Kas & bank", "Biaya operasional", "Pencairan dana"},
    "Stok & Produk": {"Semua produk", "Stok gudang", "Impor produk"},
    "Laporan Keuangan": {"Laba rugi", "Neraca", "Arus kas"},
    "Pajak": {"PPN & PPh", "Pajak marketplace"},
    "Marketplace": {"Toko marketplace", "Impor pesanan", "Penghasilan & biaya"},
    "Mitra konsinyasi": {"Daftar mitra", "Settlement"},
    "Pelanggan": {"Semua pelanggan"},
    "Pengaturan": {"Pembayaran", "Staf & akses", "Paket langganan"},
}
# Roles whose work is the full ledger start on "Menu lengkap".
FULL_MENU_ROLES = ("accountant", "finance")  # menu default only, not a permission


# The header "Buat baru" menu: every document a person starts by hand, grouped like the sidebar.
# ``query`` lands on a list page with its dialog open (``?buka=<dialog id>``, see _dialog_js.html).
# A sale is always an invoice: there is no "Pesanan" here on purpose (tests pin it).
CREATE = [
    ("Penjualan", [
        ("Invoice", "merchant-invoice-new", "file", (), "", "sales.edit"),
        ("Banyak invoice sekaligus", "merchant-invoice-batch", "layers", (), "", "sales.edit"),
        ("Penawaran", "merchant-quote-new", "note", (), "", "sales.edit"),
        ("Pesanan penjualan", "merchant-sales-order-new", "clipboard", (), "", "sales.edit"),
        ("Invoice berulang", "merchant-recurring-new", "calendar", (), "", "sales.edit"),
        ("Terima pembayaran", "merchant-receive-payment", "cash", (), "", "sales.edit"),
        ("Nota kredit", "merchant-credit-notes", "undo", (), "", "sales.edit"),  # picks the invoice first
        ("Pelanggan", "merchant-customers", "users", (), "buka=customer-dialog", "customers.edit"),
        ("Daftar harga", "merchant-price-lists", "tag", (), "buka=price-list-dialog", "sales.edit"),
    ]),
    ("Pembelian", [
        ("Pesanan pembelian", "merchant-purchase-order-new", "clipboard", (), "", "purchase.order"),
        ("Penerimaan barang", "merchant-goods-receipt-new", "box", (), "", "stock.receive"),
        ("Beli tunai", "merchant-cash-purchase", "cash", (), "", "purchase.cash"),
        ("Tagihan supplier", "merchant-bill-new", "truck", (), "", "books.edit"),
        ("Nota debit", "merchant-debit-notes", "undo", (), "", "books.edit"),  # picks the bill first
        ("Supplier", "merchant-suppliers", "handshake", (), "buka=add-supplier", "books.edit"),
    ]),
    ("Kas & bank", [
        ("Biaya operasional", "merchant-expenses", "wallet", (), "buka=expense-dialog", "books.edit"),
        ("Terima uang", "merchant-cash-new", "arrow-in", (), "kind=receive", "books.edit"),
        ("Kirim uang", "merchant-cash-new", "arrow-out", (), "kind=spend", "books.edit"),
        ("Transfer antar rekening", "merchant-cash-new", "swap", (), "kind=transfer", "books.edit"),
        ("Pinjaman", "merchant-loans", "bank", (), "buka=loan-dialog", "books.edit"),
    ]),
    ("Persediaan", [
        ("Produk", "merchant-entity-new", "tag", ("products",), "", "catalog.edit"),
        ("Stok opname", "merchant-stock-counts", "layers", (), "", "stock.count"),
        ("Penyesuaian stok", "merchant-stock-adjustment-new", "layers", (), "", "stock.adjust"),
        ("Permintaan stok", "merchant-stock-request-new", "truck", (), "", "stock.request"),
        ("Transfer antar brand", "merchant-intercompany-new", "swap", (), "", "intercompany.create"),
        ("Impor produk", "merchant-product-import", "upload", (), "", "catalog.edit"),
        ("Impor pesanan marketplace", "merchant-import", "upload", (), "", "channels.manage"),
    ]),
    ("Akuntansi", [
        ("Jurnal umum", "merchant-journal-new", "book", (), "", "books.edit"),
        ("Jurnal penyesuaian", "merchant-adjustments", "book", (), "", "books.edit"),
        ("Aset tetap", "merchant-assets", "bank", (), "buka=asset-dialog", "books.edit"),
        ("Saldo awal", "merchant-opening-balances", "clipboard", (), "", "books.edit"),
    ]),
]


def _url(name, args=()):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _score(link, path, href, query):
    """How specifically this link claims the current page (0: not at all)."""
    if link.query:
        wanted = dict(part.split("=", 1) for part in link.query.split("&"))
        if any(query.get(key) != value for key, value in wanted.items()):
            return 0
    best = 0
    for prefix in (href, *link.match):
        if prefix.startswith("="):
            if path == prefix[1:]:
                best = max(best, len(prefix) + 1000)
        elif path.startswith(prefix):
            best = max(best, len(prefix))
    return best + (500 if best and link.query else 0)


def link_key(link):
    """How ``nav_access.REFUSED`` names a link."""
    return link.url + ("/" + "/".join(map(str, link.args)) if link.args else "") + (f"?{link.query}" if link.query else "")


def build_nav(context):
    request = context.get("request")
    path = request.path if request else ""
    query = request.GET if request else {}
    role = context.get("merchant_role", "")
    store = context.get("merchant_store")
    from django.conf import settings
    flags = {"storefront": settings.STOREFRONT_ENABLED, "uses_platform_gateway": bool(store and getattr(getattr(
        store, "payment_settings", None), "use_platform_gateway", False))}
    from apps.orders.nav_access import REFUSED
    # A cashier never reaches a back-office page (their sign-in and every page they use live at
    # /kasir/, see apps/pos/views.py), so this sidebar is never built for that role; nothing to
    # special-case here.
    refused = REFUSED.get(context.get("merchant_real_role", ""), frozenset())
    from apps.billing.modules import full_books, link_open
    everything = full_books(store)
    sections, best, active = [], 0, None
    for title, groups in NAV:
        built_groups = []
        for group in groups:
            links = []
            if group.when and not flags.get(group.when):
                continue
            for link in group.links:
                if link.roles and role not in link.roles:
                    continue
                if link_key(link) in refused:
                    continue
                if not everything and not link_open(group.label, link):
                    continue
                if link.when and not flags.get(link.when):
                    continue
                href = _url(link.url, link.args)
                if href is None:
                    continue
                badge = context.get(link.badge) if link.badge else None
                item = {"label": link.label, "href": href + (f"?{link.query}" if link.query else ""),
                    "badge": badge, "active": False,
                    "core": link.label in CORE.get(group.label, ()) or bool(badge)}
                score = _score(link, path, href, query)
                if score > best:
                    best, active = score, item
                links.append(item)
            if links:
                built_groups.append({"label": group.label, "icon": group.icon, "links": links,
                    "core": any(item["core"] for item in links)})
        if built_groups:
            sections.append({"title": title, "groups": built_groups,
                "core": any(group["core"] for group in built_groups)})
    if active:
        active["active"] = True
    for section in sections:
        for group in section["groups"]:
            group["open"] = any(item["active"] for item in group["links"])
            group["active"] = group["open"]
        section["active"] = any(group["active"] for group in section["groups"])
    return sections


@register.inclusion_tag("merchant/_nav.html", takes_context=True)
def merchant_nav(context):
    request = context.get("request")
    role = context.get("merchant_real_role", "") or context.get("merchant_role", "")
    return {"sections": build_nav(context),
        "home_active": bool(request and request.path == "/books/"),
        "home_href": _url("merchant-dashboard"),
        "default_mode": "full" if role in FULL_MENU_ROLES else "simple"}


@register.inclusion_tag("merchant/_create_menu.html", takes_context=True)
def merchant_create_menu(context):
    """Only what this person may start, by the permission key each entry names (the owner is an admin)."""
    from apps.shops.permissions import holds
    role = context.get("merchant_real_role", "")
    role = "admin" if role == "owner" else role
    from apps.billing.modules import full_books, path_open
    everything = full_books(context.get("merchant_store"))
    groups = []
    for title, entries in CREATE:
        items = [{"label": label, "href": href + (f"?{query}" if query else ""), "icon": icon}
            for label, name, icon, args, query, permission in entries
            if holds(role, permission) and (href := _url(name, args)) and (everything or path_open(href))]
        if items:
            groups.append({"title": title, "items": items})
    return {"groups": groups}
