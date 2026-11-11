import uuid
from decimal import Decimal

from django.db import models

from apps.orders.models import Order
from apps.shops.models import Store


class PartnerProductPrice(models.Model):
    """Per-mitra sell-in price for a product.
    When set, this overrides product.ab_shop_price for settlement calculations
    with this specific partner. Leave blank to fall back to the product's default
    mitra price (ab_shop_price) or the retail price.
    """
    partner = models.ForeignKey(
        "ConsignmentPartner", on_delete=models.CASCADE,
        related_name="product_prices")
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE,
        related_name="partner_prices")
    price = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["partner", "product"],
                name="unique_partner_product_price")
        ]
        ordering = ["partner__name"]

    def __str__(self):
        return f"{self.partner.name} – {self.product.name}: Rp{self.price}"


class ConsignmentPartner(models.Model):
    """An external partner (e.g. AB Shop) who sells the store's products on consignment.
    Partners sell through their own channels (Shopee, Tokopedia, TV) and are invoiced
    periodically based on their actual sell-through."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="consignment_partners")
    name = models.CharField(max_length=160)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    contact_email = models.EmailField(blank=True)
    # Default payment term in days after invoice is issued
    payment_term_days = models.PositiveSmallIntegerField(default=30)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["store", "name"], name="unique_partner_name_per_store")
        ]

    def __str__(self):
        return self.name


class Settlement(models.Model):
    """
    Reconciliation statement between the store and a consignment partner for a period.

    Lifecycle:
        draft → sent → confirmed → invoiced → partially_paid → paid → closed
                           ↓
                       disputed → revised → confirmed ...

    Rules:
    - Lines and totals are locked once status reaches `confirmed`.
    - An invoice number is only assigned at `invoiced` status.
    - Payments are recorded at any point from `invoiced` onward.
    - A sell-through Order belongs to exactly one non-draft Settlement once confirmed.
    """
    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_DISPUTED = "disputed"
    STATUS_REVISED = "revised"
    STATUS_CONFIRMED = "confirmed"
    STATUS_INVOICED = "invoiced"
    STATUS_PARTIAL = "partially_paid"
    STATUS_PAID = "paid"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Dikirim ke Mitra"),
        (STATUS_DISPUTED, "Disputed"),
        (STATUS_REVISED, "Direvisi"),
        (STATUS_CONFIRMED, "Dikonfirmasi"),
        (STATUS_INVOICED, "Sudah Ditagih"),
        (STATUS_PARTIAL, "Dibayar Sebagian"),
        (STATUS_PAID, "Lunas"),
        (STATUS_CLOSED, "Closed"),
    ]

    # Transitions allowed: {from_status: [allowed_to_statuses]}
    TRANSITIONS = {
        STATUS_DRAFT:     [STATUS_SENT],
        STATUS_SENT:      [STATUS_CONFIRMED, STATUS_DISPUTED],
        STATUS_DISPUTED:  [STATUS_REVISED],
        STATUS_REVISED:   [STATUS_SENT],
        STATUS_CONFIRMED: [STATUS_INVOICED],
        STATUS_INVOICED:  [STATUS_PARTIAL, STATUS_PAID],
        STATUS_PARTIAL:   [STATUS_PAID],
        STATUS_PAID:      [STATUS_CLOSED],
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="settlements")
    partner = models.ForeignKey(ConsignmentPartner, on_delete=models.PROTECT,
        related_name="settlements")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT,
        db_index=True)
    # Invoice number assigned when status moves to invoiced
    invoice_number = models.CharField(max_length=80, blank=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)

    # Totals (computed and stored when lines are saved; recalculated on line changes until confirmed)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    shipping_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    total_due = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))

    # Notes visible to the partner (shown on settlement statement)
    notes = models.TextField(blank=True)
    # Internal notes (not shown to partner)
    internal_notes = models.TextField(blank=True)
    # Dispute reason (required when moving to disputed)
    dispute_reason = models.TextField(blank=True)

    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_settlements")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    invoiced_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-period_end", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(period_end__gte=models.F("period_start")),
                name="settlement_period_end_gte_start"),
            models.CheckConstraint(condition=models.Q(subtotal__gte=0),
                name="settlement_subtotal_nonnegative"),
            models.CheckConstraint(condition=models.Q(total_due__gte=0),
                name="settlement_total_due_nonnegative"),
            models.CheckConstraint(condition=models.Q(amount_paid__gte=0),
                name="settlement_amount_paid_nonnegative"),
        ]

    def __str__(self):
        return f"{self.partner} · {self.period_start}–{self.period_end} [{self.get_status_display()}]"

    @property
    def balance_due(self):
        return self.total_due - self.amount_paid

    @property
    def is_locked(self):
        """Lines cannot be edited once confirmed."""
        return self.status not in (self.STATUS_DRAFT, self.STATUS_SENT,
                                   self.STATUS_DISPUTED, self.STATUS_REVISED)

    def can_transition_to(self, new_status):
        return new_status in self.TRANSITIONS.get(self.status, [])

    def recalculate_totals(self):
        """Recompute subtotal, shipping_total, total_due from lines. Call before save."""
        agg = self.lines.aggregate(
            sub=models.Sum("line_total"),
            ship=models.Sum("shipping_contribution"),
        )
        self.subtotal = agg["sub"] or Decimal("0")
        self.shipping_total = agg["ship"] or Decimal("0")
        self.total_due = self.subtotal + self.shipping_total


class SettlementLine(models.Model):
    """One sell-through order included in a settlement."""
    settlement = models.ForeignKey(Settlement, on_delete=models.CASCADE, related_name="lines")
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="settlement_line",
        null=True, blank=True)
    # Snapshot fields (denormalised so the settlement is self-contained even if order changes)
    description = models.CharField(max_length=300)
    # Date the downstream sale occurred (from the sell-through order)
    sale_date = models.DateField()
    # Channel AB Shop sold through for this line
    ab_shop_source = models.CharField(max_length=20, blank=True)
    # Totals for this order at AB Shop prices
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    # Shipping charged to partner for this order
    shipping_contribution = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["sale_date", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(line_total__gte=0),
                name="settlement_line_total_nonnegative"),
            models.CheckConstraint(condition=models.Q(shipping_contribution__gte=0),
                name="settlement_line_shipping_nonnegative"),
        ]

    def __str__(self):
        return f"{self.sale_date} · {self.description} · Rp{self.line_total}"


class SettlementPayment(models.Model):
    """A payment received from the partner against a settlement."""
    METHODS = [
        ("bank_transfer", "Transfer Bank"),
        ("cash", "Tunai"),
        ("other", "Lainnya"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    settlement = models.ForeignKey(Settlement, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_date = models.DateField()
    method = models.CharField(max_length=20, choices=METHODS, default="bank_transfer")
    reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["payment_date", "created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0),
                name="settlement_payment_amount_positive"),
        ]

    def __str__(self):
        return f"Rp{self.amount} · {self.payment_date} · {self.get_method_display()}"
