"""Hak akses: the one table of who may do what.

Every module used to declare its own role sets (``EDIT_ROLES = {"admin", "finance"}``) and they drifted.
Now they are all derived from ``MATRIX``: a view asks for ``roles("books.edit")`` and the "Hak akses"
page prints this same table, so what an owner reads is what the server enforces. The store owner
always passes, and ``accountant`` is read-only everywhere (see ``orders.views._require_roles``).

Segregation of duties is separate from roles: ``maker_may_not_check`` stops one person from both
creating and paying a bill, unless nobody else could (a one-person store is never locked out)."""

ROLE_LABELS = {"admin": "Admin", "manager": "Manajer", "finance": "Keuangan", "ops": "Operasional",
    "warehouse": "Gudang", "customer_service": "Layanan pelanggan", "viewer": "Pelihat",
    "bookkeeper": "Akuntan", "ar_staff": "Staf piutang (AR)", "ap_staff": "Staf utang (AP)",
    "tax_staff": "Staf pajak", "accountant": "Auditor"}
ROLE_ORDER = ["admin", "manager", "finance", "bookkeeper", "ar_staff", "ap_staff", "tax_staff", "ops", "warehouse",
    "customer_service", "viewer", "accountant"]

# The accounting roles were added after the screens were written, and the screens decide which buttons to
# show from a handful of older role names. Each accounting role therefore *looks* like one of those on
# screen, while the server checks its own row of MATRIX: a button it may not use answers 404.
SCREEN_ROLE = {"bookkeeper": "finance", "ar_staff": "finance", "ap_staff": "finance", "tax_staff": "finance",
    "accountant": "viewer"}

# permission -> (group, label, roles). Reading financial pages by `accountant` is granted in
# `_require_roles` for anything `viewer` or `finance` can read; it never writes.
MATRIX = {
    "books.view": ("Pembukuan", "Melihat laporan, jurnal, utang, dan piutang", {"admin", "manager", "finance", "viewer", "bookkeeper", "ar_staff", "ap_staff", "tax_staff"}),
    "books.edit": ("Pembukuan", "Mencatat tagihan, pembayaran, biaya, jurnal, dan menerima barang ke tagihan", {"admin", "finance", "bookkeeper", "ap_staff"}),
    "approval.decide": ("Pembukuan", "Menyetujui permintaan bernilai besar", {"admin", "manager", "finance"}),
    "period.close": ("Pembukuan", "Menutup periode dan tahun buku", {"admin", "finance", "bookkeeper"}),
    "period.reopen": ("Pembukuan", "Membuka kembali tahun yang ditutup", {"admin"}),
    "intercompany.create": ("Pembukuan", "Transfer barang antar brand", {"admin"}),
    "tax.view": ("Pajak", "Melihat PPN, PPh, dan lembar kerja pajak", {"admin", "manager", "finance", "viewer", "bookkeeper", "tax_staff"}),
    "tax.edit": ("Pajak", "Mengubah profil pajak dan menyetor PPh", {"admin", "finance", "bookkeeper", "tax_staff"}),
    "sales.view": ("Penjualan", "Melihat invoice, penawaran, dan penerimaan", {"admin", "manager", "ops", "finance", "viewer", "bookkeeper", "ar_staff", "tax_staff"}),
    "sales.edit": ("Penjualan", "Membuat dan mengubah invoice dan penawaran", {"admin", "manager", "ops", "finance", "bookkeeper", "ar_staff"}),
    "orders.manage": ("Penjualan", "Membuat dan mengubah pesanan manual", {"admin", "manager", "ops", "customer_service"}),
    "giftcards.manage": ("Penjualan", "Mengelola kartu hadiah", {"admin", "manager", "customer_service"}),
    "settlement.manage": ("Mitra", "Settlement dan pembayaran mitra konsinyasi", {"admin", "manager", "finance"}),
    "settlement.view": ("Mitra", "Melihat settlement mitra", {"admin", "manager", "finance", "viewer", "bookkeeper"}),
    "stock.fulfil": ("Stok", "Memproses pesanan dan stok keluar", {"admin", "manager", "ops", "warehouse"}),
    "purchase.order": ("Stok", "Membuat dan menutup pesanan pembelian ke supplier", {"admin", "manager", "finance", "ops", "bookkeeper", "ap_staff"}),
    "stock.count": ("Stok", "Membuat dan mengisi hasil stok opname", {"admin", "manager", "ops", "warehouse"}),
    "stock.count_approve": ("Stok", "Menyetujui stok opname dan mencatat selisihnya", {"admin", "manager", "finance"}),
    "stock.request": ("Stok", "Meminta stok dari gudang lain dan menerima kiriman", {"admin", "manager", "ops", "warehouse"}),
    "stock.request_approve": ("Stok", "Menyetujui atau menolak permintaan stok antar gudang", {"admin", "manager"}),
    "stock.send": ("Stok", "Mengirim barang untuk permintaan stok yang disetujui", {"admin", "manager", "ops", "warehouse"}),
    "stock.oversell": ("Stok", "Menjual melebihi stok (stok minus), bila toko mengizinkannya", {"admin", "manager"}),
    "stock.receive": ("Stok", "Menerima barang dari supplier", {"admin", "manager", "finance", "warehouse", "bookkeeper", "ap_staff"}),
    "customers.view": ("Pelanggan", "Melihat pelanggan dan segmen", {"admin", "manager", "ops", "customer_service", "viewer", "ar_staff"}),
    "customers.edit": ("Pelanggan", "Mengubah pelanggan", {"admin", "manager", "customer_service"}),
    "customers.broadcast": ("Pelanggan", "Mengirim siaran ke pelanggan", {"admin", "manager"}),
    "channels.manage": ("Marketplace", "Impor pesanan dan kelola channel", {"admin", "manager", "ops"}),
    "channels.mapping": ("Marketplace", "Memetakan SKU marketplace", {"admin", "manager", "ops"}),
    "channels.income": ("Marketplace", "Impor laporan penghasilan", {"admin", "finance", "bookkeeper"}),
    "reports.view": ("Laporan", "Melihat laporan penjualan dan margin", {"admin", "manager", "finance", "ops", "viewer", "bookkeeper", "ar_staff", "ap_staff", "tax_staff"}),
    "recap.view": ("Laporan", "Melihat rekap pengiriman", {"admin", "manager", "ops", "warehouse", "customer_service", "viewer"}),
    "settings.manage": ("Pengaturan", "Mengubah pengaturan toko dan pengiriman", {"admin", "manager"}),
    "billing.manage": ("Pengaturan", "Mengelola langganan dan tagihan platform", {"admin", "finance"}),
    "staff.manage": ("Pengaturan", "Mengundang staf dan mengubah peran", {"admin"}),
}


def screen_role(role):
    """The role name the templates and the sidebar should see for ``role``."""
    return SCREEN_ROLE.get(role, role)


def roles(permission):
    """The roles that hold ``permission`` (a frozenset, so a module cannot change it by accident)."""
    return frozenset(MATRIX[permission][2])


def holds(role, permission):
    return role in MATRIX[permission][2]


def role_of(user, store):
    """``"owner"``, a staff role, or ``None``."""
    if store.owner_id == user.id:
        return "owner"
    member = store.staff_members.filter(user=user).first()
    return member.role if member else None


def can(user, store, permission):
    role = role_of(user, store)
    return role == "owner" or (role is not None and holds(role, permission))


def holders(store, permission):
    """Users who can perform ``permission``: the owner and staff whose role holds it."""
    users = {store.owner}
    users |= {m.user for m in store.staff_members.filter(role__in=roles(permission)).select_related("user")}
    return users


def maker_may_not_check(store, actor, maker, permission="books.edit"):
    """True when ``actor`` would be checking their own work: they made the document and someone
    else could do the checking. A store with nobody else is never blocked."""
    if maker is None or actor.id != getattr(maker, "id", maker):
        return False
    return bool(holders(store, permission) - {actor})


def grouped():
    """The matrix for display: ``[(group, [(permission, label, {role: bool})])]``."""
    out = {}
    for permission, (group, label, allowed) in MATRIX.items():
        out.setdefault(group, []).append((permission, label, {r: r in allowed for r in ROLE_ORDER}))
    return list(out.items())
