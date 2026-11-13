import uuid
from decimal import Decimal

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
        (STATUS_ISSUED,  "Belum Dibayar"),
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
    # Sharing: a secret link the buyer opens without logging in, and when it was sent / opened.
    public_token = models.CharField(max_length=40, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
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
    def credited(self):
        """Nota kredit that reduced what the buyer owes on this invoice."""
        return sum((note.applied for note in self.credit_notes.all()
            if note.status != CreditNote.STATUS_VOID), Decimal(0))

    @property
    def amount_due(self):
        return max(self.total - self.amount_paid - self.credited, Decimal(0))


class InvoiceItem(models.Model):
    """One line on a sales invoice, copied from OrderItem at issue time."""
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    dimension = models.CharField(max_length=60, blank=True, help_text="Ukuran/dimensi, mis. 100×200cm")
    unit = models.CharField(max_length=20, blank=True, help_text="Satuan, mis. pcs, box, kg, jam")
    note = models.CharField(max_length=300, blank=True, help_text="Keterangan tambahan di bawah nama barang")
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
    METHOD_CREDIT     = "credit"
    METHOD_OTHER      = "other"
    METHOD_CHOICES = [
        (METHOD_CASH,        "Tunai"),
        (METHOD_TRANSFER,    "Transfer Bank"),
        (METHOD_QRIS,        "QRIS"),
        (METHOD_COD,         "COD"),
        (METHOD_MARKETPLACE, "Marketplace"),
        (METHOD_CREDIT,      "Kredit pelanggan"),
        (METHOD_OTHER,       "Lainnya"),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    # Which of the store's money accounts received it (the main cash account when empty).
    bank_account = models.ForeignKey("accounting.BankAccount", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+")
    # One transfer that paid several invoices is a receipt; each invoice gets its share.
    receipt = models.ForeignKey("CustomerReceipt", on_delete=models.PROTECT, null=True,
        blank=True, related_name="allocations")
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


# ---------------------------------------------------------------------------
# Document numbers
# ---------------------------------------------------------------------------

class DocumentSequence(models.Model):
    """Last number issued per store, kind of document (CN, QT, RC...) and year; locked while
    issuing so numbers have no gaps."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="document_sequences")
    kind = models.CharField(max_length=8)
    year = models.PositiveSmallIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "kind", "year"],
            name="unique_document_sequence")]


# ---------------------------------------------------------------------------
# Nota kredit: a correction to an invoice already issued
# ---------------------------------------------------------------------------

class CreditNote(models.Model):
    """Reduces an invoice without cancelling it: goods returned, a price corrected, a discount
    granted afterwards. It lowers the sales, the PPN keluaran and what the buyer owes; anything
    beyond what they owe is refunded or kept as their credit."""
    STATUS_ISSUED, STATUS_VOID = "issued", "void"
    STATUS_CHOICES = [(STATUS_ISSUED, "Diterbitkan"), (STATUS_VOID, "Dibatalkan")]
    REASONS = [("return", "Barang diretur"), ("price", "Koreksi harga"),
        ("discount", "Diskon susulan"), ("damaged", "Barang rusak / tidak sesuai"),
        ("other", "Lainnya")]
    EXCESS_CREDIT, EXCESS_REFUND, EXCESS_PLATFORM = "credit", "refund", "platform"
    EXCESS_CHOICES = [(EXCESS_CREDIT, "Simpan sebagai kredit pelanggan"),
        (EXCESS_REFUND, "Kembalikan uang"),
        (EXCESS_PLATFORM, "Dipotong dari pencairan marketplace")]
    MARKETPLACE_CHANNELS = {"shopee", "tokopedia", "tiktok_shop", "lazada"}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="credit_notes")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ISSUED)
    issued_on = models.DateField()
    reason = models.CharField(max_length=10, choices=REASONS, default="return")
    notes = models.TextField(blank=True)
    buyer_name = models.CharField(max_length=160)
    buyer_npwp = models.CharField(max_length=16, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    # The part that reduces the invoice's balance; the rest (``paid_out``) is money the buyer
    # had already paid, refunded or kept as credit.
    applied = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_out = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    excess_action = models.CharField(max_length=10, choices=EXCESS_CHOICES, default=EXCESS_CREDIT)
    bank_account = models.ForeignKey("accounting.BankAccount", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_on", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["store", "number"], name="unique_store_credit_note_number"),
            models.CheckConstraint(condition=models.Q(total__gt=0), name="credit_note_positive"),
        ]

    def __str__(self):
        return self.number


class CreditNoteItem(models.Model):
    note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name="items")
    invoice_item = models.ForeignKey(InvoiceItem, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="credited_lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    restock = models.BooleanField(default=False)  # goods come back to the shelf
    restocked_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]


# ---------------------------------------------------------------------------
# Customer receipts and credit
# ---------------------------------------------------------------------------

class CustomerReceipt(models.Model):
    """Money received from one buyer, spread over several invoices. What no invoice takes
    stays as the buyer's credit (uang muka), to be applied to a later invoice or refunded."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="customer_receipts")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    buyer_name = models.CharField(max_length=160)
    number = models.CharField(max_length=40)
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    deposit = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # kept as credit
    method = models.CharField(max_length=20, choices=InvoicePayment.METHOD_CHOICES,
        default=InvoicePayment.METHOD_TRANSFER)
    reference = models.CharField(max_length=120, blank=True)
    bank_account = models.ForeignKey("accounting.BankAccount", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+")
    note = models.CharField(max_length=200, blank=True)
    voided = models.BooleanField(default=False)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "number"], name="unique_store_receipt_number"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="receipt_positive"),
        ]


class CustomerCredit(models.Model):
    """One movement of a buyer's credit balance: positive when credit is created (an advance
    payment, an overpayment, a credit note), negative when it is used on an invoice or
    refunded. The balance is the sum of the movements that stand."""
    KINDS = [("deposit", "Uang muka / kelebihan bayar"), ("credit_note", "Nota kredit"),
        ("applied", "Dipakai untuk invoice"), ("refund", "Dikembalikan")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="customer_credits")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    buyer_name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=KINDS)
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # signed
    note = models.CharField(max_length=200, blank=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    credit_note = models.ForeignKey(CreditNote, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    receipt = models.ForeignKey(CustomerReceipt, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    payment = models.OneToOneField(InvoicePayment, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="credit_movement")
    voided = models.BooleanField(default=False)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]


# ---------------------------------------------------------------------------
# Penawaran (quotes) and recurring invoices
# ---------------------------------------------------------------------------

class Quote(models.Model):
    STATUS_DRAFT, STATUS_SENT, STATUS_ACCEPTED = "draft", "sent", "accepted"
    STATUS_DECLINED, STATUS_INVOICED = "declined", "invoiced"
    STATUS_CHOICES = [(STATUS_DRAFT, "Draft"), (STATUS_SENT, "Terkirim"),
        (STATUS_ACCEPTED, "Disetujui"), (STATUS_DECLINED, "Ditolak"),
        (STATUS_INVOICED, "Sudah diinvoice")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="quotes")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    issued_on = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    buyer_name = models.CharField(max_length=160)
    company_name = models.CharField(max_length=160, blank=True)
    buyer_npwp = models.CharField(max_length=16, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    invoice = models.OneToOneField(Invoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="quote")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_on", "-number"]
        constraints = [models.UniqueConstraint(fields=["store", "number"],
            name="unique_store_quote_number")]

    def __str__(self):
        return self.number


class QuoteItem(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class RecurringInvoice(models.Model):
    """An invoice that repeats (rent, a monthly supply, a subscription): the worker issues it
    on credit on each due date and sets the next one."""
    INTERVALS = [("week", "Mingguan"), ("month", "Bulanan"), ("quarter", "Per 3 bulan"),
        ("year", "Tahunan")]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="recurring_invoices")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    name = models.CharField(max_length=160)
    buyer_name = models.CharField(max_length=160)
    company_name = models.CharField(max_length=160, blank=True)
    buyer_npwp = models.CharField(max_length=16, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    interval = models.CharField(max_length=8, choices=INTERVALS, default="month")
    next_run = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    due_days = models.PositiveIntegerField(default=14)
    is_active = models.BooleanField(default=True)
    runs = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_run", "id"]


class RecurringInvoiceItem(models.Model):
    recurring = models.ForeignKey(RecurringInvoice, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class RecurringRun(models.Model):
    """One invoice generated by a recurring template; unique per template and date so the
    worker running twice issues nothing twice."""
    recurring = models.ForeignKey(RecurringInvoice, on_delete=models.CASCADE, related_name="history")
    run_date = models.DateField()
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")

    class Meta:
        ordering = ["-run_date"]
        constraints = [models.UniqueConstraint(fields=["recurring", "run_date"],
            name="unique_recurring_run")]
