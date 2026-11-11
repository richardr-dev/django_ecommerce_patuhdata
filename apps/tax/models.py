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
    STATUS_DRAFT   = "draft"
    STATUS_ISSUED  = "issued"
    STATUS_PAID    = "paid"
    STATUS_PARTIAL = "partial"
    STATUS_OVERDUE = "overdue"
    STATUS_VOID    = "void"
    STATUS_CHOICES = [
        (STATUS_DRAFT,   "Draft"),
        (STATUS_ISSUED,  "Diterbitkan"),
        (STATUS_PAID,    "Lunas"),
        (STATUS_PARTIAL, "Bayar Sebagian"),
        (STATUS_OVERDUE, "Jatuh Tempo"),
        (STATUS_VOID,    "Dibatalkan"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="invoices")
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ISSUED)
    issued_on = models.DateField()
    due_date = models.DateField(null=True, blank=True)
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
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_on", "-number"]
        constraints = [models.UniqueConstraint(fields=["store", "number"],
            name="unique_store_invoice_number")]

    def __str__(self):
        return self.number

    @property
    def amount_paid(self):
        return sum(p.amount for p in self.payments.all())

    @property
    def amount_due(self):
        from decimal import Decimal
        return self.total - self.amount_paid


class InvoiceItem(models.Model):
    """One line on a sales invoice, copied from OrderItem at issue time."""
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    dimension = models.CharField(max_length=60, blank=True, help_text="Ukuran/dimensi, mis. 100×200cm")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} ×{self.quantity}"


class InvoicePayment(models.Model):
    """A payment applied against an invoice (full or partial)."""
    METHOD_CASH       = "cash"
    METHOD_TRANSFER   = "transfer"
    METHOD_QRIS       = "qris"
    METHOD_COD        = "cod"
    METHOD_MARKETPLACE = "marketplace"
    METHOD_OTHER      = "other"
    METHOD_CHOICES = [
        (METHOD_CASH,        "Tunai"),
        (METHOD_TRANSFER,    "Transfer Bank"),
        (METHOD_QRIS,        "QRIS"),
        (METHOD_COD,         "COD"),
        (METHOD_MARKETPLACE, "Marketplace"),
        (METHOD_OTHER,       "Lainnya"),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_TRANSFER)
    reference = models.CharField(max_length=120, blank=True,
        help_text="Nomor referensi/bukti transfer atau kode transaksi.")
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["paid_on", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="invoice_payment_positive"),
        ]

    def __str__(self):
        return f"{self.paid_on} Rp{self.amount}"
