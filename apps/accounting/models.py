import uuid

from django.db import models
from django.utils.deconstruct import deconstructible

from apps.catalog.private_files import private_storage_callable
from apps.orders.models import PaymentTransaction, Refund
from apps.shops.models import Store


class Account(models.Model):
    TYPES = [("asset", "Aset"), ("liability", "Liabilitas"), ("equity", "Ekuitas"),
        ("revenue", "Pendapatan"), ("expense", "Beban")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="ledger_accounts")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=TYPES)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["store", "code"],
            name="unique_store_account_code")]

    def __str__(self):
        return f"{self.code} {self.name}"


class Payout(models.Model):
    """Money a payment gateway or marketplace transferred to the seller's bank, settling the
    orders it lists. Each payout reference is recorded once per provider."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="payouts")
    provider = models.CharField(max_length=40)
    reference = models.CharField(max_length=120)
    paid_on = models.DateField()
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    fees = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=14, decimal_places=2)
    # The fees by ledger account ({"6110": "1500.00", …}) when the marketplace's income report
    # itemised them; empty for a plain payout file, whose fees all go to the general account.
    fee_breakdown = models.JSONField(default=dict, blank=True)
    # The money account the payout landed in (the main cash account when empty), so it can be
    # matched to that account's bank statement.
    bank_account = models.ForeignKey("BankAccount", on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        constraints = [models.UniqueConstraint(fields=["store", "provider", "reference"],
            name="unique_store_payout_reference")]

    def __str__(self):
        return f"{self.provider} {self.reference}"


class PayoutLine(models.Model):
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name="lines")
    # A payment is settled by exactly one payout.
    payment = models.OneToOneField(PaymentTransaction, on_delete=models.PROTECT,
        related_name="payout_line")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)


# Operating expenses the seller records by hand (Biaya operasional), one ledger account each.
EXPENSE_CATEGORIES = [
    ("6200", "Gaji & upah"),
    ("6300", "Sewa tempat"),
    ("6400", "Iklan & promosi"),
    ("6500", "Kemasan & perlengkapan"),
    ("6600", "Listrik, air & internet"),
    ("6700", "Transportasi & ongkos kirim"),
    ("6800", "Langganan aplikasi & software"),
    ("6900", "Biaya lain-lain"),
]


@deconstructible
class ExpenseReceiptPath:
    def __call__(self, instance, filename):
        import uuid

        safe = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or "nota"
        return f"expenses/{instance.store.slug}/{uuid.uuid4().hex}/{safe}"


class Expense(models.Model):
    """An operating expense paid from the seller's bank (rent, salaries, ads...). Posted to the
    ledger when saved, so the profit and loss shows real net profit."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="expenses")
    date = models.DateField()
    category = models.CharField(max_length=10, choices=EXPENSE_CATEGORIES)
    description = models.CharField(max_length=200)
    vendor = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # what left the bank
    input_vat = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # PPN masukan
    receipt = models.FileField(upload_to=ExpenseReceiptPath(), storage=private_storage_callable,
        blank=True)
    # The money account it was paid from (the main cash account when empty).
    cost_center = models.ForeignKey("CostCenter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses", verbose_name="Pusat biaya")
    # True while the expense waits for a second person to approve it: it is not on the books yet.
    approval_pending = models.BooleanField(default=False)
    bank_account = models.ForeignKey("BankAccount", on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="expense_positive"),
            models.CheckConstraint(condition=models.Q(input_vat__gte=0)
                & models.Q(input_vat__lt=models.F("amount")), name="expense_vat_within_amount"),
        ]

    def __str__(self):
        return f"{self.date} {self.description}"


class JournalEntry(models.Model):
    """A balanced double-entry posting.

    Every entry names the business document that caused it as ``source_type`` + ``source_id``
    (e.g. ``invoice`` + the invoice id). A document has at most one live entry, so posting is
    idempotent and any module (invoicing, purchasing, POS...) can post without the ledger
    importing its models. Entries are never edited: a correction supersedes the entry with a
    reversing one. The payment/refund/payout/expense links predate ``source`` and are kept for
    the pages that follow them."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="journal_entries")
    date = models.DateField()
    memo = models.CharField(max_length=300)
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    # True once a reversing entry cancels this one; the document may then be posted again.
    superseded = models.BooleanField(default=False)
    reverses = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="reversal")
    payment = models.OneToOneField(PaymentTransaction, on_delete=models.PROTECT, null=True,
        blank=True, related_name="journal_entry")
    refund = models.OneToOneField(Refund, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    payout = models.OneToOneField(Payout, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    expense = models.OneToOneField(Expense, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    cost_center = models.ForeignKey("CostCenter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="entries")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name_plural = "journal entries"
        constraints = [models.UniqueConstraint(fields=["store", "source_type", "source_id"],
            condition=models.Q(superseded=False) & ~models.Q(source_type=""),
            name="unique_live_entry_per_source")]
        indexes = [models.Index(fields=["store", "date"], name="journal_store_date")]


class LedgerLock(models.Model):
    """Tutup buku: no entry may be posted, reversed or removed on or before this date, so a
    period already reported to the tax office cannot change afterwards."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="ledger_lock")
    locked_through = models.DateField()
    locked_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [models.CheckConstraint(name="journal_line_one_sided",
            condition=models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0))]


# ---------------------------------------------------------------------------
# Accounts Payable (AP) — supplier bills and payments
# ---------------------------------------------------------------------------

class Supplier(models.Model):
    """A vendor or supplier the store buys from."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="suppliers")
    name = models.CharField(max_length=160)
    contact = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    npwp = models.CharField(max_length=20, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["store", "name"], name="unique_store_supplier_name")]

    def __str__(self):
        return self.name


# What a supplier bill buys: stock for resale, or one of the operating expense accounts.
BILL_CATEGORIES = [("1400", "Pembelian barang dagang (persediaan)")] + EXPENSE_CATEGORIES


class Bill(models.Model):
    """A supplier invoice (AP invoice). Tracks money owed TO suppliers."""
    STATUS_DRAFT   = "draft"
    STATUS_OPEN    = "open"
    STATUS_PAID    = "paid"
    STATUS_PARTIAL = "partial"
    STATUS_OVERDUE = "overdue"
    STATUS_VOID    = "void"
    STATUS_CHOICES = [
        (STATUS_DRAFT,   "Draft"),
        (STATUS_OPEN,    "Belum Dibayar"),
        (STATUS_PAID,    "Lunas"),
        (STATUS_PARTIAL, "Bayar Sebagian"),
        (STATUS_OVERDUE, "Jatuh Tempo"),
        (STATUS_VOID,    "Dibatalkan"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="bills")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="bills")
    number = models.CharField(max_length=40, blank=True, help_text="Nomor faktur dari supplier")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)
    category = models.CharField(max_length=10, choices=BILL_CATEGORIES, default="1400")
    bill_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    cost_center = models.ForeignKey("CostCenter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bills", verbose_name="Pusat biaya")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-bill_date", "-created_at"]

    def __str__(self):
        return f"Bill {self.number or str(self.id)[:8]} — {self.supplier.name}"

    @property
    def amount_paid(self):
        from decimal import Decimal
        return sum((p.amount for p in self.payments.all()), Decimal(0))

    @property
    def amount_due(self):
        return self.total - self.amount_paid


class BillItem(models.Model):
    """One line on a supplier bill."""
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    # Goods bought: the product (and variant) this line puts on the shelf once received.
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    received_quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} ×{self.quantity}"


class BillPayment(models.Model):
    """A payment made against a supplier bill."""
    METHOD_CHOICES = [
        ("transfer", "Transfer Bank"),
        ("cash", "Tunai"),
        ("other", "Lainnya"),
    ]
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="payments")
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="transfer")
    reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=200, blank=True)
    bank_account = models.ForeignKey("BankAccount", on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["paid_on", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="bill_payment_positive")]


# ---------------------------------------------------------------------------
# Kas & bank: accounts, money in/out, bank statements and reconciliation
# ---------------------------------------------------------------------------

class BankAccount(models.Model):
    """A place money sits: a bank account, petty cash, or an e-wallet. Each has its own ledger
    account (1100 for the first, then 1101, 1102...), so balances and reconciliation are per
    account while reports still add them up as cash."""
    KINDS = [("bank", "Rekening bank"), ("cash", "Kas tunai"), ("ewallet", "E-wallet")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="bank_accounts")
    account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name="bank_account")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=KINDS, default="bank")
    bank_name = models.CharField(max_length=60, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["account__code"]

    def __str__(self):
        return self.name


class CashTransaction(models.Model):
    """Terima uang, kirim uang, or a transfer between the store's own accounts: money that moves
    without an invoice or bill (owner capital, bank interest, admin fees, a cash purchase)."""
    RECEIVE, SPEND, TRANSFER = "receive", "spend", "transfer"
    KINDS = [(RECEIVE, "Terima uang"), (SPEND, "Kirim uang"), (TRANSFER, "Transfer antar rekening")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="cash_transactions")
    kind = models.CharField(max_length=10, choices=KINDS)
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="+")
    to_bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True,
        blank=True, related_name="+")
    date = models.DateField()
    contact = models.CharField(max_length=160, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=300, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # PPN on the lines
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    voided = models.BooleanField(default=False)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(total__gt=0),
            name="cash_transaction_positive")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.total}"


class CashTransactionLine(models.Model):
    transaction = models.ForeignKey(CashTransaction, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class BankStatementLine(models.Model):
    """One line of a bank statement (mutasi rekening) imported from the bank's CSV. Matching it
    to the ledger line it stands for is what reconciles the account."""
    UNMATCHED, MATCHED, IGNORED = "unmatched", "matched", "ignored"
    STATUSES = [(UNMATCHED, "Belum dicocokkan"), (MATCHED, "Cocok"), (IGNORED, "Diabaikan")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE,
        related_name="statement_lines")
    date = models.DateField()
    description = models.CharField(max_length=300)
    reference = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # + money in, − money out
    balance = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    import_key = models.CharField(max_length=64)  # the same mutation is imported only once
    status = models.CharField(max_length=10, choices=STATUSES, default=UNMATCHED)
    matched_line = models.OneToOneField(JournalLine, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="statement_line")
    matched_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    matched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        constraints = [models.UniqueConstraint(fields=["bank_account", "import_key"],
            name="unique_statement_line")]


# ---------------------------------------------------------------------------
# Aset tetap: fixed assets and their monthly straight-line depreciation
# ---------------------------------------------------------------------------

# Kelompok harta berwujud bukan bangunan (PMK 72/2023), with their useful life in years.
ASSET_GROUPS = [
    ("1", "Kelompok 1 · 4 tahun (laptop, HP, perabot ringan)", 4),
    ("2", "Kelompok 2 · 8 tahun (kendaraan, mesin ringan, AC)", 8),
    ("3", "Kelompok 3 · 16 tahun (mesin produksi berat)", 16),
    ("4", "Kelompok 4 · 20 tahun (alat berat)", 20),
    ("b", "Bangunan permanen · 20 tahun", 20),
    ("x", "Lainnya (masa manfaat sendiri)", 0),
]


class FixedAsset(models.Model):
    ACTIVE, DISPOSED = "active", "disposed"
    STATUSES = [(ACTIVE, "Aktif"), (DISPOSED, "Dilepas")]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="fixed_assets")
    name = models.CharField(max_length=160)
    number = models.CharField(max_length=40, blank=True)
    group = models.CharField(max_length=2, choices=[(c, label) for c, label, _ in ASSET_GROUPS],
        default="1")
    acquired_on = models.DateField()
    cost = models.DecimalField(max_digits=14, decimal_places=2)
    residual_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    useful_life_months = models.PositiveIntegerField()
    # Where the purchase was paid from (a cash/bank account, utang usaha, or modal pemilik).
    paid_from = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=STATUSES, default=ACTIVE)
    disposed_on = models.DateField(null=True, blank=True)
    disposal_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    disposal_to = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-acquired_on", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(cost__gt=0), name="asset_cost_positive"),
            models.CheckConstraint(condition=models.Q(residual_value__gte=0)
                & models.Q(residual_value__lt=models.F("cost")), name="asset_residual_below_cost"),
            models.CheckConstraint(condition=models.Q(useful_life_months__gt=0),
                name="asset_life_positive"),
        ]

    def __str__(self):
        return self.name


class CostCenter(models.Model):
    """A branch, outlet or department that costs are tracked and budgeted for."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="cost_centers")
    code = models.CharField(max_length=12)
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_cost_center_code")]

    def __str__(self):
        return f"{self.code} · {self.name}"


class BudgetLine(models.Model):
    """What the store planned to spend (or earn) on one account in one month, optionally for one
    cost centre. Compared with the ledger in Anggaran vs realisasi."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="budget_lines")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="budget_lines")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.CASCADE, null=True, blank=True,
        related_name="budget_lines")
    amount = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["store", "year", "month", "account", "cost_center"],
                name="unique_budget_line"),
            models.UniqueConstraint(fields=["store", "year", "month", "account"],
                condition=models.Q(cost_center__isnull=True), name="unique_budget_line_no_center"),
            models.CheckConstraint(condition=models.Q(month__gte=1, month__lte=12), name="budget_month_valid"),
        ]


@deconstructible
class AttachmentPath:
    def __call__(self, instance, filename):
        import uuid

        safe = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or "lampiran"
        return f"attachments/{instance.store.slug}/{instance.kind}/{uuid.uuid4().hex}/{safe}"


class Attachment(models.Model):
    """A supporting document (supplier invoice, receipt, transfer proof, contract) kept with the
    record it supports, for the audit. Stored privately: only the store's team can open it."""
    KINDS = [("bill", "Tagihan supplier"), ("journal", "Jurnal"), ("cash", "Transaksi kas"),
        ("invoice", "Invoice penjualan")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=12, choices=KINDS)
    object_id = models.CharField(max_length=64)
    file = models.FileField(upload_to=AttachmentPath(), storage=private_storage_callable)
    name = models.CharField(max_length=160)
    size = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["store", "kind", "object_id"])]


class ApprovalPolicy(models.Model):
    """From what amount an action needs a second person to approve it (maker and checker)."""
    KINDS = [("bill_payment", "Pembayaran tagihan supplier"), ("expense", "Biaya operasional"),
        ("journal", "Jurnal manual"), ("invoice_void", "Pembatalan invoice"),
        ("bill_void", "Pembatalan tagihan supplier")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="approval_policies")
    kind = models.CharField(max_length=20, choices=KINDS)
    threshold = models.DecimalField(max_digits=16, decimal_places=2, default=0,
        help_text="Persetujuan diminta untuk jumlah sebesar ini atau lebih. 0 berarti semuanya.")
    enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "kind"], name="unique_approval_policy")]


class ApprovalRequest(models.Model):
    """A pending action: what was asked, by whom, and what was decided. The action runs only when a
    different person approves it, and runs as the person who asked."""
    PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
    STATUS = [(PENDING, "Menunggu"), (APPROVED, "Disetujui"), (REJECTED, "Ditolak")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="approval_requests")
    kind = models.CharField(max_length=20, choices=ApprovalPolicy.KINDS)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    summary = models.CharField(max_length=300)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUS, default=PENDING, db_index=True)
    requester = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, related_name="+")
    decided_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
