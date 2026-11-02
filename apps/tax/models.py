import uuid

from django.db import models

from apps.orders.models import Order
from apps.shops.models import Store


class TaxProfile(models.Model):
    """Who the seller is to the tax office. Drives the invoice header and the monthly report."""
    TAXPAYER_TYPES = [("individual", "Orang pribadi"), ("company", "Badan usaha (PT/CV/koperasi)")]
    PPH_REGIMES = [("umkm_final", "PPh Final UMKM 0,5% (PP 55/2022)"),
        ("normal", "Tarif umum (pembukuan)")]
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="tax_profile")
    legal_name = models.CharField(max_length=160, blank=True,
        help_text="Nama sesuai NPWP. Kosongkan untuk memakai nama toko.")
    npwp = models.CharField(max_length=16, blank=True,
        help_text="NPWP 16 digit (orang pribadi: NIK). NPWP lama 15 digit otomatis diawali 0.")
    address = models.TextField(blank=True, help_text="Alamat sesuai NPWP, dicetak di invoice.")
    taxpayer_type = models.CharField(max_length=20, choices=TAXPAYER_TYPES, default="individual")
    pph_regime = models.CharField(max_length=20, choices=PPH_REGIMES, default="umkm_final")
    is_pkp = models.BooleanField(default=False, verbose_name="Pengusaha Kena Pajak (PKP)",
        help_text="Hanya PKP yang boleh memungut PPN dan menerbitkan Faktur Pajak.")
    invoice_prefix = models.SlugField(max_length=12, default="INV",
        help_text="Awalan nomor invoice, misalnya INV menjadi INV/2026/000001.")

    def __str__(self):
        return f"Profil pajak {self.store}"


class InvoiceSequence(models.Model):
    """The last number issued per store and year, locked while issuing so numbers have no gaps."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="invoice_sequences")
    year = models.PositiveSmallIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "year"],
            name="unique_invoice_sequence")]


class Invoice(models.Model):
    """The sales invoice of a paid order. Seller and buyer details are copied at issue time so a
    later profile change never rewrites an invoice already sent."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="invoices")
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    number = models.CharField(max_length=40)
    issued_on = models.DateField()
    seller_name = models.CharField(max_length=160)
    seller_npwp = models.CharField(max_length=16, blank=True)
    seller_address = models.TextField(blank=True)
    seller_is_pkp = models.BooleanField(default=False)
    buyer_name = models.CharField(max_length=160)
    buyer_address = models.TextField(blank=True)
    buyer_npwp = models.CharField(max_length=16, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_base = models.DecimalField(max_digits=14, decimal_places=2)  # DPP
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_on", "-number"]
        constraints = [models.UniqueConstraint(fields=["store", "number"],
            name="unique_store_invoice_number")]

    def __str__(self):
        return self.number
