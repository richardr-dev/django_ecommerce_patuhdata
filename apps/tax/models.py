import uuid
from decimal import Decimal

from django.core.validators import FileExtensionValidator
from django.db import models

from apps.orders.models import Order
from apps.shops.models import Store, hex_color
from apps.shops.uploads import StoreUploadPath


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
    # How the seller's documents look: invoice, surat tagihan, surat jalan, quote, credit note.
    invoice_logo = models.FileField(upload_to=StoreUploadPath("invoice"), blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Logo di invoice dan surat tagihan. Kosongkan untuk memakai logo toko.")
    invoice_accent = models.CharField(max_length=7, default="#1a66d9", validators=[hex_color],
        help_text="Warna judul dan tabel di invoice.")
    invoice_show_name = models.BooleanField(default=True,
        help_text="Matikan jika logo sudah memuat nama usaha.")
    invoice_footer = models.TextField(blank=True, max_length=600,
        help_text="Dicetak di bawah invoice: rekening, syarat pembayaran, ucapan terima kasih.")

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
    # The buyer pressed "Saya sudah transfer" on the public page: the seller checks the bank.
    transfer_claimed_at = models.DateTimeField(null=True, blank=True)
    transfer_claim_note = models.CharField(max_length=300, blank=True)
    # Made from surat jalan of a sales order: stock already left and its cost sits in 1410, so this
    # invoice books HPP against 1410, takes no stock, and returns none when voided.
    from_deliveries = models.BooleanField(default=False)
    # Cash rounding given (negative) or taken (positive) at the register; part of ``total`` and booked
    # to Pembulatan penjualan (4950), never to Penjualan.
    rounding = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # The outlet or branch this sale counts for; its journal entries carry it.
    cost_center = models.ForeignKey("accounting.CostCenter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invoices")
    sales_order = models.ForeignKey("SalesOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    # The location the goods were sold from ("Jual dari gudang", a register's outlet); empty: the store's
    # locations in priority order. An edit takes the new goods from the same place.
    warehouse = models.ForeignKey("catalog.Warehouse", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    # The prices were typed with PPN in them ("Harga sudah termasuk PPN"): the edit form shows them that
    # way again and backs PPN out once more, so an edit keeps the total the seller typed.
    prices_include_tax = models.BooleanField(default=False)
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
    def has_line_discount(self):
        """True when any line carries its own discount: documents then show a Diskon column."""
        return any(item.discount for item in self.items.all())

    @property
    def amount_due(self):
        return max(self.total - self.amount_paid - self.credited, Decimal(0))


class InvoiceItem(models.Model):
    """One line on a sales invoice, copied from OrderItem at issue time."""
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+")
    # The size or colour sold; its stock and cost are the variant's, ``product`` is its parent.
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    dimension =models.CharField(max_length=60, blank=True, help_text="Ukuran/dimensi, mis. 100×200cm")
    unit = models.CharField(max_length=20, blank=True, help_text="Satuan, mis. pcs, box, kg, jam")
    # Base units in one ``unit`` (a dus of 12 pcs: 12). Stock moves quantity × unit_factor base units.
    unit_factor = models.PositiveIntegerField(default=1)
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
    # Taken at a register: the acquirer holds the money until it settles to the bank, so these wait in
    # Piutang EDC / QRIS (1210) whatever bank account is named.
    METHOD_EDC        = "edc"
    METHOD_QRIS_POS   = "qris_pos"
    # Not money: the balance given up as a bad debt (tax/writeoff.py).
    METHOD_WRITEOFF   = "writeoff"
    CLEARING_METHODS  = {METHOD_EDC, METHOD_QRIS_POS}
    METHOD_CHOICES = [
        (METHOD_CASH,        "Tunai"),
        (METHOD_TRANSFER,    "Transfer Bank"),
        (METHOD_QRIS,        "QRIS"),
        (METHOD_COD,         "COD"),
        (METHOD_MARKETPLACE, "Marketplace"),
        (METHOD_CREDIT,      "Kredit pelanggan"),
        (METHOD_OTHER,       "Lainnya"),
        (METHOD_EDC,         "Kartu debit/kredit (EDC)"),
        (METHOD_QRIS_POS,    "QRIS kasir"),
        (METHOD_WRITEOFF,    "Hapus piutang"),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    # Which of the store's money accounts received it (the main cash account when empty).
    bank_account = models.ForeignKey("accounting.BankAccount", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+")
    # One transfer that paid several invoices is a receipt; each invoice gets its share.
    receipt = models.ForeignKey("CustomerReceipt", on_delete=models.PROTECT, null=True,
        blank=True, related_name="allocations")
    paid_on = models.DateField()
    # What this payment settles of the invoice. Money received is amount - withheld.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    # PPh the buyer held back and pays to the tax office for us (PPh 23 on services, 4(2) on rent). We hold
    # a bukti potong instead of the cash; it books to PPh dibayar di muka, not to the bank.
    withheld = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Paid from a taxed uang muka: the PPN already charged on it comes off PPN keluaran with this payment.
    vat_settled = models.DecimalField(max_digits=14, decimal_places=2, default=0)
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
            models.CheckConstraint(condition=models.Q(withheld__gte=0, withheld__lte=models.F("amount")),
                name="invoice_payment_withheld_within_amount"),
        ]

    @property
    def received(self):
        return self.amount - self.withheld

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
    restocked_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # per base unit
    unit_factor = models.PositiveIntegerField(default=1)  # base units in one of ``quantity`` (the invoice line's)

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
    # Uang muka kena PPN: the PPN inside ``deposit`` (faktur uang muka), booked to PPN keluaran on receipt.
    deposit_vat = models.DecimalField(max_digits=14, decimal_places=2, default=0)
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

    @property
    def deposit_base(self):
        """DPP of a taxed uang muka: the deposit without its PPN."""
        return self.deposit - self.deposit_vat

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
    # Signed PPN inside ``amount`` that was already charged (a taxed uang muka); Kredit pelanggan holds amount - vat.
    vat = models.DecimalField(max_digits=14, decimal_places=2, default=0)
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
    # Sharing: the buyer's link (/q/<token>/), when it went out, was opened and answered.
    public_token = models.CharField(max_length=40, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    response_note = models.CharField(max_length=300, blank=True)
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
    # The size or colour offered; ``product`` is its parent.
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    @property
    def pick_id(self):
        """The value the line editor's product picker uses: ``v<id>`` for a variant."""
        return f"v{self.variant_id}" if self.variant_id else str(self.product_id)

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


class FiscalAdjustment(models.Model):
    """A line of the annual corporate tax worksheet: a positive or negative koreksi fiskal (commercial
    profit to taxable income), or a tax credit (PPh 22/23/25 already paid or withheld). Entered by the
    accountant with a note, never derived, so the worksheet shows exactly what was claimed and why."""
    POSITIVE, NEGATIVE, CREDIT = "positive", "negative", "credit"
    KINDS = [(POSITIVE, "Koreksi positif (menambah laba kena pajak)"), (NEGATIVE, "Koreksi negatif (mengurangi laba kena pajak)"),
        (CREDIT, "Kredit pajak (PPh 22/23/25 yang sudah dibayar)")]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="fiscal_adjustments")
    year = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=KINDS)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "id"]


class TaxFiling(models.Model):
    """The accountant's tracker for a tax duty that has been done (deposited and/or reported): which
    duty (``code``, e.g. ``ppn:2026-10``), when, and the reference (NTPN / BPE)."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="tax_filings")
    code = models.CharField(max_length=40)
    done_on = models.DateField()
    reference = models.CharField(max_length=80, blank=True)
    done_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_tax_filing")]


class ReminderPolicy(models.Model):
    """Automatic payment reminders for a store (tax/reminders.py): N days before the due date and on
    chosen days after it, by email and through the store's own WhatsApp account."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="reminder_policy")
    enabled = models.BooleanField(default=False)
    days_before = models.PositiveSmallIntegerField(default=3, help_text="0: tidak ada pengingat sebelum jatuh tempo.")
    days_after = models.CharField(max_length=60, default="1,7,14", help_text="Hari setelah jatuh tempo, dipisah koma.")
    use_email = models.BooleanField(default=True)
    use_whatsapp = models.BooleanField(default=True)
    # Tahan kredit: no new credit sale to a customer with an invoice more than N days past due (tax/credit_limit.py).
    credit_hold_days = models.PositiveSmallIntegerField(null=True, blank=True,
        help_text="Kosong: tidak ditahan. 0: tahan begitu ada invoice lewat jatuh tempo.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Pengingat {self.store_id}"


class ReminderLog(models.Model):
    """One row per reminder step actually attempted, so a step never goes out twice."""
    BEFORE, AFTER = "before", "after"
    EMAIL, WHATSAPP = "email", "whatsapp"
    PENDING, SENT, FAILED = "pending", "sent", "failed"
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=10)
    offset = models.PositiveSmallIntegerField()
    channel = models.CharField(max_length=10)
    to = models.CharField(max_length=254, blank=True)
    status = models.CharField(max_length=10, default=PENDING)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["invoice", "kind", "offset", "channel"],
            name="unique_reminder_step")]


class SalesOrder(models.Model):
    """Pesanan penjualan: what the customer asked for, at agreed prices. Books nothing, holds no stock.
    Delivered through ``Delivery`` (surat jalan), invoiced from deliveries (tax/sales_orders.py)."""
    STATUS_OPEN, STATUS_PARTIAL, STATUS_FULFILLED = "open", "partial", "fulfilled"
    STATUS_CLOSED, STATUS_CANCELLED = "closed", "cancelled"
    STATUS_CHOICES = [(STATUS_OPEN, "Terbuka"), (STATUS_PARTIAL, "Sebagian terkirim"),
        (STATUS_FULFILLED, "Terkirim semua"), (STATUS_CLOSED, "Ditutup"), (STATUS_CANCELLED, "Dibatalkan")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="sales_orders")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_orders")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)
    issued_on = models.DateField()
    expected_on = models.DateField(null=True, blank=True)
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
    quote = models.OneToOneField(Quote, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_order")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_on", "-number"]
        constraints = [models.UniqueConstraint(fields=["store", "number"], name="unique_store_sales_order_number")]

    def __str__(self):
        return self.number

    @property
    def ordered_units(self):
        return sum(line.quantity for line in self.lines.all())

    @property
    def delivered_units(self):
        return sum(line.delivered_quantity for line in self.lines.all())


class SalesOrderLine(models.Model):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    # The size or colour ordered; ``product`` is its parent. Its delivery takes that variant's units.
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    description = models.CharField(max_length=200)
    sku = models.CharField(max_length=80, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, blank=True)
    delivered_quantity = models.PositiveIntegerField(default=0)
    invoiced_quantity = models.PositiveIntegerField(default=0)

    @property
    def reserved_quantity(self):
        return sum(row.quantity for row in self.reservations.all())

    class Meta:
        ordering = ["id"]

    @property
    def remaining(self):
        return max(0, self.quantity - self.delivered_quantity)

    @property
    def line_total(self):
        return self.quantity * self.unit_price - self.discount


class SalesOrderReservation(models.Model):
    """Units held for an open sales order line: on one warehouse level (a product) or store-wide (a variant).
    Released when they are delivered, or when the order is closed or cancelled. See ``tax/sales_orders.py``."""
    line = models.ForeignKey("SalesOrderLine", on_delete=models.CASCADE, related_name="reservations")
    inventory_level = models.ForeignKey("catalog.InventoryLevel", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Delivery(models.Model):
    """Surat jalan for a sales order: the stock document. Units leave the shelf when it is made."""
    STATUS_SHIPPED, STATUS_CANCELLED = "shipped", "cancelled"
    STATUS_CHOICES = [(STATUS_SHIPPED, "Terkirim"), (STATUS_CANCELLED, "Dibatalkan")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="deliveries")
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="deliveries")
    number = models.CharField(max_length=40)
    delivered_on = models.DateField()
    courier = models.CharField(max_length=40, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SHIPPED)
    # The location the parcel left from ("Kirim dari gudang"); empty: the locations in priority order.
    # Cancelling puts the units back there.
    warehouse = models.ForeignKey("catalog.Warehouse", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="deliveries")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-delivered_on", "-number"]
        constraints = [models.UniqueConstraint(fields=["store", "number"], name="unique_store_delivery_number")]

    def __str__(self):
        return self.number

    @property
    def value(self):
        return sum((line.unit_cost * line.quantity for line in self.lines.all()), Decimal(0))


class DeliveryLine(models.Model):
    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name="lines")
    order_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT, related_name="deliveries")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]

    @property
    def value(self):
        return self.unit_cost * self.quantity
