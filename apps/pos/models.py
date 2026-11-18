"""Kasir (POS): registers, shifts and receipts of a physical outlet. See docs/pos-architecture.md.

Nothing here is a second set of books. A receipt is one ordinary invoice (``channel="pos"``) made by
``tax/sales.py``; its lines are that invoice's lines, its tenders are that invoice's payments. These
models only add what a shop floor needs on top: which drawer, which shift, who sold, what was
handed over and what change was given."""
import uuid

from django.conf import settings
from django.db import models

from apps.shops.models import Store


class PosOutletSettings(models.Model):
    """How one outlet sells at the register."""
    ROUNDING = [(0, "Ke rupiah penuh"), (100, "Turun ke Rp100"), (500, "Turun ke Rp500")]
    outlet = models.OneToOneField("catalog.Warehouse", on_delete=models.CASCADE, related_name="pos_settings")
    # Retail shelf prices include PPN; the books still split Penjualan and PPN keluaran.
    prices_include_tax = models.BooleanField(default=True, verbose_name="Harga sudah termasuk PPN")
    rounding_step = models.PositiveSmallIntegerField(choices=ROUNDING, default=0, verbose_name="Pembulatan")
    # A cashier may discount up to this share of a line or receipt; more needs pos.discount_override.
    max_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=10,
        verbose_name="Batas diskon kasir (%)")
    receipt_header = models.CharField(max_length=200, blank=True, verbose_name="Kepala struk")
    receipt_footer = models.CharField(max_length=200, blank=True, verbose_name="Kaki struk")

    def __str__(self):
        return f"POS {self.outlet.name}"


class PosRegister(models.Model):
    """A cash drawer in an outlet (not a device: two tablets may serve one register). It has its own
    cash account in the books, so the drawer's money is counted apart from every other account."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="pos_registers")
    outlet = models.ForeignKey("catalog.Warehouse", on_delete=models.PROTECT, related_name="pos_registers")
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=80)
    cash_account = models.OneToOneField("accounting.BankAccount", on_delete=models.PROTECT,
        related_name="pos_register")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["outlet__name", "code"]
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_pos_register_code")]

    def __str__(self):
        return f"{self.outlet.name} · {self.name}"


class PosShift(models.Model):
    """One cashier session on a register, from the counted float to the counted close."""
    OPEN, CLOSED = "open", "closed"
    STATUS = [(OPEN, "Buka"), (CLOSED, "Ditutup")]
    register = models.ForeignKey(PosRegister, on_delete=models.PROTECT, related_name="shifts")
    status = models.CharField(max_length=10, choices=STATUS, default=OPEN)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    opened_at = models.DateTimeField(auto_now_add=True)
    opening_float = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    # What the books say the drawer holds at the close, what was counted, and the difference
    # (negative: short, posted to Selisih kas kasir; positive: over, posted to Lebih kas kasir).
    expected_cash = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deposited = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    closed_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    # True while a variance above the store's threshold has no live entry yet: waiting for a
    # different person to decide (``accounting.approvals``, kind ``pos_shift_variance``), or
    # already rejected. Only an approval flips it back to false and posts the entry; the backlog
    # poster skips every shift here on purpose, so a rejection is never auto-posted later.
    variance_pending_approval = models.BooleanField(default=False)
    variance_request = models.ForeignKey("accounting.ApprovalRequest", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")

    class Meta:
        ordering = ["-opened_at"]
        constraints = [models.UniqueConstraint(fields=["register"], condition=models.Q(status="open"),
            name="one_open_shift_per_register")]

    def __str__(self):
        return f"Shift {self.register.code} {self.opened_at:%d/%m/%Y %H:%M}"

    @property
    def is_open(self):
        return self.status == self.OPEN


class PosSale(models.Model):
    """One receipt (struk). The amounts and lines live on its invoice."""
    PAID, VOID = "paid", "void"
    STATUS = [(PAID, "Lunas"), (VOID, "Dibatalkan")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="pos_sales")
    register = models.ForeignKey(PosRegister, on_delete=models.PROTECT, related_name="sales")
    shift = models.ForeignKey(PosShift, on_delete=models.PROTECT, related_name="sales")
    number = models.CharField(max_length=40)
    invoice = models.OneToOneField("tax.Invoice", on_delete=models.PROTECT, related_name="pos_sale")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("shops.Customer", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pos_sales")
    status = models.CharField(max_length=10, choices=STATUS, default=PAID)
    sold_at = models.DateTimeField(auto_now_add=True)
    total = models.DecimalField(max_digits=14, decimal_places=2)       # what the buyer owed, after rounding
    tendered = models.DecimalField(max_digits=14, decimal_places=2)    # what was handed over
    change = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # The cart as rung up, at shelf prices (what the receipt prints): the invoice keeps the same lines
    # before PPN. ``[{"name", "sku", "qty", "price", "discount", "total"}]``; ``discount`` is the receipt's own.
    cart = models.JSONField(default=list, blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    token = models.CharField(max_length=40, blank=True, db_index=True)  # e-receipt link
    # The browser's key for this receipt: a double tap or a retried request returns the first sale.
    idem_key = models.CharField(max_length=64, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-sold_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "number"], name="unique_pos_sale_number"),
            models.UniqueConstraint(fields=["store", "idem_key"], condition=~models.Q(idem_key=""),
                name="unique_pos_sale_idem"),
        ]

    def __str__(self):
        return self.number


class PosTender(models.Model):
    """One way a receipt was paid; a split payment has several."""
    CASH, QRIS, EDC, TRANSFER = "cash", "qris", "edc", "transfer"
    METHODS = [(CASH, "Tunai"), (QRIS, "QRIS"), (EDC, "Kartu debit/kredit"), (TRANSFER, "Transfer bank")]
    sale = models.ForeignKey(PosSale, on_delete=models.CASCADE, related_name="tenders")
    method = models.CharField(max_length=10, choices=METHODS)
    amount = models.DecimalField(max_digits=14, decimal_places=2)      # applied to the receipt (cash: less the change)
    tendered = models.DecimalField(max_digits=14, decimal_places=2)    # handed over
    reference = models.CharField(max_length=120, blank=True)           # approval code, QRIS reference
    payment = models.OneToOneField("tax.InvoicePayment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="pos_tender")

    class Meta:
        ordering = ["id"]
