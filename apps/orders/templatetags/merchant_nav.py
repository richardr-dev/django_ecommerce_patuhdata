"""The back-office sidebar, defined once.

Sections group the work the way a seller thinks about it: selling (sales, marketplaces, mitra,
purchasing), inventory, accounting (books, reports, tax), customers and marketing, and settings.
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


ADMIN = frozenset({"admin"})

NAV = [
    ("Bisnis", [
        Group("Penjualan", "receipt", [
            Link("Ringkasan penjualan", "merchant-sales", match=("=/merchant/sales/",
                "/merchant/sales/stats/")),
            Link("Analitik penjualan", "merchant-sales-analytics"),
            Link("Pesanan", "merchant-orders", match=("/merchant/orders/",)),
            # Creating an invoice is one button on this page (and in "Buat baru"), not a menu item.
            Link("Invoice penjualan", "merchant-sales-invoices",
                match=("/merchant/sales/invoices/",)),
            Link("Penawaran", "merchant-quotes"),
            Link("Retur & penukaran", "merchant-returns"),
            Link("Rekap produksi", "merchant-production-recap"),
        ]),
        Group("Piutang & pembayaran", "cash", [
            Link("Piutang pelanggan", "merchant-receivables"),
            Link("Terima pembayaran", "merchant-receive-payment",
                match=("/merchant/sales/receipts/",)),
            Link("Kredit pelanggan", "merchant-customer-credit"),
            Link("Nota kredit", "merchant-credit-notes"),
            Link("Invoice berulang", "merchant-recurring"),
        ]),
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
        Group("Pembelian", "truck", [
            Link("Supplier", "merchant-suppliers"),
            Link("Tagihan supplier", "merchant-bills"),
            Link("Buat tagihan", "merchant-bill-new"),
            Link("Umur utang", "merchant-payables"),
        ]),
    ]),
    ("Inventori", [
        Group("Stok & Produk", "box", [
            Link("Semua produk", "merchant-entity-list", ("products",), match=("/merchant/products/",)),
            Link("Kategori", "merchant-entity-list", ("categories",)),
            Link("Paket (bundle)", "merchant-bundles"),
            Link("Stok gudang", "merchant-stock"),
            Link("Gudang & outlet", "merchant-entity-list", ("warehouses",)),
            Link("Lokasi stok", "merchant-entity-list", ("inventory",)),
            Link("Impor produk", "merchant-product-import"),
            Link("Foto produk", "merchant-entity-list", ("images",)),
        ]),
    ]),
    ("Akuntansi", [
        Group("Akuntansi", "book", [
            Link("Ringkasan", "merchant-accounting", match=("=/merchant/accounting/",)),
            Link("Kas & bank", "merchant-bank-accounts", match=("/merchant/accounting/cash/",)),
            Link("Jurnal", "merchant-journal-list"),
            Link("Pemeriksaan pembukuan", "merchant-books-checks"),
            Link("Persetujuan", "merchant-approvals", badge="approvals_pending"),
            Link("Anggaran", "merchant-budget", match=("/merchant/accounting/budget/",)),
            Link("Pusat biaya", "merchant-cost-centers"),
            Link("Saldo awal", "merchant-opening-balances"),
            Link("Jurnal umum", "merchant-journal-new"),
            Link("Bagan akun", "merchant-chart-of-accounts"),
            Link("Nilai persediaan", "merchant-inventory-value"),
            Link("Aset tetap", "merchant-assets"),
            Link("Biaya operasional", "merchant-expenses"),
            Link("Pencairan dana", "merchant-payouts"),
            Link("Saldo DOKU Lakuin", "merchant-platform-balance", when="uses_platform_gateway"),
        ]),
        Group("Laporan Keuangan", "bar-chart", [
            Link("Laba rugi", "merchant-profit-loss"),
            Link("Arus kas", "merchant-cash-flow"),
            Link("Buku besar & neraca saldo", "merchant-finance"),
            Link("Neraca", "merchant-balance-sheet"),
            Link("Neraca saldo", "merchant-trial-balance"),
            Link("Rekap invoice per channel", "merchant-channel-recap"),
            Link("Ekspor data penjualan", "merchant-sales-export"),
            Link("Penjualan semua channel", "merchant-sales-overview"),
            Link("Penjualan bulanan", "merchant-sales-report"),
        ]),
        Group("Pajak", "percent", [
            Link("PPN & PPh", "merchant-tax"),
            Link("SPT Masa PPN", "merchant-spt-ppn"),
            Link("PPh dipotong", "merchant-withholdings", match=("/merchant/tax/pph/",)),
            Link("Tampilan invoice", "merchant-invoice-style", roles=frozenset({"admin", "manager", "finance"})),
        ]),
    ]),
    ("Pelanggan & pemasaran", [
        Group("Pelanggan", "users", [
            Link("Semua pelanggan", "merchant-customers", badge="pending_customers_count"),
            Link("Broadcast", "merchant-broadcasts"),
            Link("Keranjang tertinggal", "merchant-engagement"),
            Link("Ulasan", "merchant-entity-list", ("reviews",)),
        ]),
        Group("Promosi", "megaphone", [
            Link("Flash sale & popup", "merchant-entity-list", ("campaigns",)),
            Link("Produk flash sale", "merchant-entity-list", ("flash-sales",)),
            Link("Voucher", "merchant-entity-list", ("coupons",)),
            Link("Gift card", "merchant-gift-cards"),
            Link("Link kampanye", "merchant-campaign-links"),
            Link("Feed & QR katalog", "merchant-feeds"),
        ]),
        Group("Toko online", "store", [
            Link("Tema, logo & banner", "merchant-settings"),
            Link("Atur banner", "merchant-entity-list", ("slides",)),
            Link("Halaman brand", "merchant-entity-list", ("pages",)),
        ]),
    ]),
    ("", [
        Group("Pengaturan", "sliders", [
            Link("Pembayaran", "merchant-payment-settings"),
            Link("Pengiriman", "merchant-entity-list", ("shipping",), match=("/merchant/shipping-areas/",)),
            Link("Jadwal & pre-order", "merchant-delivery"),
            Link("WhatsApp", "merchant-whatsapp", roles=ADMIN),
            Link("Integrasi", "merchant-integrations"),
            Link("Pelacakan & pixel", "merchant-tracking"),
            Link("Staf & akses", "merchant-staff", roles=ADMIN),
            Link("Log aktivitas", "merchant-activity", roles=ADMIN),
            Link("Paket langganan", "merchant-billing"),
        ]),
    ]),
]

# Shortcuts of the header "Buat baru" menu.
CREATE = [
    ("Invoice", "merchant-invoice-new", "file"),
    ("Penawaran", "merchant-quote-new", "note"),
    ("Terima pembayaran", "merchant-receive-payment", "cash"),
    ("Tagihan supplier", "merchant-bill-new", "truck"),
    ("Biaya operasional", "merchant-expenses", "wallet"),
    ("Terima uang", "merchant-cash-new", "arrow-in"),
    ("Jurnal umum", "merchant-journal-new", "book"),
    ("Impor pesanan marketplace", "merchant-import", "upload"),
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


def build_nav(context):
    request = context.get("request")
    path = request.path if request else ""
    query = request.GET if request else {}
    role = context.get("merchant_role", "")
    store = context.get("merchant_store")
    flags = {"uses_platform_gateway": bool(store and getattr(getattr(
        store, "payment_settings", None), "use_platform_gateway", False))}
    sections, best, active = [], 0, None
    for title, groups in NAV:
        built_groups = []
        for group in groups:
            links = []
            for link in group.links:
                if link.roles and role not in link.roles:
                    continue
                if link.when and not flags.get(link.when):
                    continue
                href = _url(link.url, link.args)
                if href is None:
                    continue
                item = {"label": link.label, "href": href + (f"?{link.query}" if link.query else ""),
                    "badge": context.get(link.badge) if link.badge else None, "active": False}
                score = _score(link, path, href, query)
                if score > best:
                    best, active = score, item
                links.append(item)
            if links:
                built_groups.append({"label": group.label, "icon": group.icon, "links": links})
        if built_groups:
            sections.append({"title": title, "groups": built_groups})
    if active:
        active["active"] = True
    for section in sections:
        for group in section["groups"]:
            group["open"] = any(item["active"] for item in group["links"])
    return sections


@register.inclusion_tag("merchant/_nav.html", takes_context=True)
def merchant_nav(context):
    request = context.get("request")
    return {"sections": build_nav(context),
        "home_active": bool(request and request.path == "/merchant/"),
        "home_href": _url("merchant-dashboard")}


@register.inclusion_tag("merchant/_create_menu.html")
def merchant_create_menu():
    return {"items": [{"label": label, "href": _url(name), "icon": icon}
        for label, name, icon in CREATE if _url(name)]}
