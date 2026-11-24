"""The new seller's setup checklist on the dashboard. Each step is ticked from the store's real
data, so it stays right however the seller got there."""
from dataclasses import dataclass

from django.urls import reverse

from apps.shops.availability import setup_steps as live_steps
from apps.shops.tenancy import store_url


@dataclass
class SetupStep:
    title: str
    hint: str
    done: bool
    url: str
    action: str
    required: bool = False  # shoppers see the store only once every required step is done


def setup_steps(store):
    # The required steps are the ones apps/shops/availability.py checks before going live.
    live = {step["key"]: step["done"] for step in live_steps(store)}
    return [
        SetupStep("Pilih tema & logo", "Warna, huruf, dan logo sesuai brand Anda.",
            bool(store.storefront_template or store.logo), reverse("merchant-settings"),
            "Pilih tema"),
        SetupStep("Pasang banner", "Foto landscape yang tampil di bagian atas toko.",
            bool(store.cover_image) or store.storefront_slides.filter(is_active=True).exists(),
            reverse("merchant-settings") + "#banner", "Unggah banner"),
        SetupStep("Tambah produk pertama", "Nama, harga, stok, dan beberapa foto sekaligus.",
            live["product"], reverse("merchant-entity-new", args=["products"]), "Tambah produk",
            required=True),
        SetupStep("Aktifkan pembayaran", "Transfer bank bisa langsung dipakai hari ini.",
            live["payment"], reverse("merchant-payment-settings"), "Atur pembayaran",
            required=True),
        SetupStep("Atur pengiriman", "Pilih lokasi gudang di peta agar ongkir JNE, J&T, dan "
            "kurir lain dihitung otomatis, atau buat tarif toko sendiri.", live["shipping"],
            _warehouse_url(store), "Atur lokasi gudang", required=True),
        SetupStep("Bagikan toko & terima pesanan", "Kirim tautan toko ke WhatsApp dan media sosial.",
            store.orders.exists(), store_url(store), "Buka toko"),
    ]


def _warehouse_url(store):
    warehouse = store.warehouses.filter(is_active=True).order_by("priority", "id").first()
    return (reverse("merchant-entity-edit", args=["warehouses", warehouse.id]) if warehouse
        else reverse("merchant-entity-new", args=["warehouses"]))


MAIN_ACCOUNT_NAME = "Kas & bank utama"  # the name `accounting.bank.bank_accounts` gives the automatic one


def books_setup_steps(store):
    """Starting the books, in the order a shop does it: who it is to the tax office, where its money
    is, what it sells and at what cost, the balances on the first day, then the first sale. Each step
    is ticked from real data, like the shop list above, and its hint says what is still missing."""
    from apps.accounting import opening
    from apps.accounting.models import BankAccount
    from apps.catalog.models import Product
    from apps.tax.models import Invoice, TaxProfile

    profile = TaxProfile.objects.filter(store=store).first()
    if profile:
        regime = "PPh Final 0,5% dari omzet" if profile.pph_regime == "umkm_final" else "PPh tarif umum"
        tax_hint = (f"Saat ini: {profile.get_taxpayer_type_display().lower()}, {regime}, "
            f"{'PKP (memungut PPN)' if profile.is_pkp else 'bukan PKP'}."
            + ("" if profile.npwp else " Periksa, lalu isi NPWP atau NIK."))
    else:
        tax_hint = "Orang pribadi atau badan, PPh Final 0,5% atau tarif umum, PKP atau bukan, dan NPWP."

    accounts = list(BankAccount.objects.filter(store=store, is_active=True).values_list("name", flat=True))
    # The main cash account is created automatically, so it alone does not mean the step was done.
    banks_done = len(accounts) > 1 or (accounts and accounts[0] != MAIN_ACCOUNT_NAME)

    products = Product.objects.filter(store=store, is_active=True, is_digital=False)
    no_cost = products.filter(cost_price__isnull=True).count()
    if not products.exists():
        product_hint = "Impor dari Excel (CSV): nama, SKU, harga jual, stok, dan harga pokok sekaligus."
    elif no_cost:
        product_hint = (f"{no_cost} produk belum punya harga pokok, jadi HPP dan laba belum akurat. "
            "Isi kolom cost_price lalu impor ulang.")
    else:
        product_hint = "Semua produk punya harga pokok."

    return [
        SetupStep("Periksa profil pajak", tax_hint, bool(profile and profile.npwp),
            reverse("merchant-tax"), "Isi profil pajak"),
        SetupStep("Catat kas & rekening bank", "Laci kas, setiap rekening bank, dan e-wallet (QRIS, "
            "GoPay, OVO) yang menerima uang usaha.", bool(banks_done),
            reverse("merchant-bank-accounts"), "Tambah rekening"),
        SetupStep("Masukkan produk dan harga pokok", product_hint,
            products.exists() and not no_cost, reverse("merchant-product-import"), "Impor produk"),
        SetupStep("Isi saldo awal", "Uang di kas dan bank, nilai stok, piutang, dan utang pada hari Anda "
            "mulai memakai pembukuan.", opening.existing(store) is not None,
            reverse("merchant-opening-balances"), "Isi saldo awal"),
        SetupStep("Catat penjualan pertama", "Dari kasir, invoice, atau impor pesanan marketplace; "
            "penjualan, HPP, dan stok tercatat otomatis.", Invoice.objects.filter(store=store).exists(),
            reverse("merchant-invoice-new"), "Buat invoice"),
    ]
