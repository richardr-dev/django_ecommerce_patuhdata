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
