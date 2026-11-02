"""The new seller's setup checklist on the dashboard. Each step is ticked from the store's real
data, so it stays right however the seller got there."""
from dataclasses import dataclass

from django.urls import reverse

from apps.integrations.payments import payment_options
from apps.shops.tenancy import store_url

from .models import ShippingMethod


@dataclass
class SetupStep:
    title: str
    hint: str
    done: bool
    url: str
    action: str


def setup_steps(store):
    return [
        SetupStep("Pilih tema & logo", "Warna, huruf, dan logo sesuai brand Anda.",
            bool(store.storefront_template or store.logo), reverse("merchant-settings"),
            "Pilih tema"),
        SetupStep("Pasang banner", "Foto landscape yang tampil di bagian atas toko.",
            bool(store.cover_image) or store.storefront_slides.filter(is_active=True).exists(),
            reverse("merchant-settings") + "#banner", "Unggah banner"),
        SetupStep("Tambah produk pertama", "Nama, harga, stok, dan beberapa foto sekaligus.",
            store.products.filter(is_active=True).exists(),
            reverse("merchant-entity-new", args=["products"]), "Tambah produk"),
        SetupStep("Aktifkan pembayaran", "Transfer bank bisa langsung dipakai hari ini.",
            bool(payment_options(store)), reverse("merchant-payment-settings"),
            "Atur pembayaran"),
        SetupStep("Atur pengiriman", "Ongkir yang dibayar pembeli saat checkout.",
            ShippingMethod.objects.filter(store=store, is_active=True).exists(),
            reverse("merchant-entity-list", args=["shipping"]), "Atur ongkir"),
        SetupStep("Bagikan toko & terima pesanan", "Kirim tautan toko ke WhatsApp dan media sosial.",
            store.orders.exists(), store_url(store), "Buka toko"),
    ]
