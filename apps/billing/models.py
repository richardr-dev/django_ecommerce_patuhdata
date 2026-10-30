import uuid

from django.db import models

from apps.shops.models import Store


class Plan(models.Model):
    """A seller subscription tier. Prices are IDR; a null limit means unlimited."""
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=60)
    tagline = models.CharField(max_length=160, blank=True)
    price_monthly = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    price_yearly = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    max_products = models.PositiveIntegerField(null=True, blank=True,
        help_text="Produk aktif maksimal. Kosongkan untuk tanpa batas.")
    allow_custom_domain = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False,
        help_text="Paket untuk toko baru dan toko yang langganannya berakhir.")
    is_public = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_monthly"]
        constraints = [
            models.UniqueConstraint(fields=["is_default"], condition=models.Q(is_default=True),
                name="single_default_plan"),
            models.CheckConstraint(condition=models.Q(price_monthly__gte=0, price_yearly__gte=0),
                name="plan_price_non_negative"),
        ]

    def __str__(self): return self.name

    @property
    def is_paid(self): return self.price_monthly > 0

    def price_for(self, months):
        return self.price_yearly if months == 12 else self.price_monthly


class Subscription(models.Model):
    """A store's plan. Stores without one were created by the platform operator and are
    not limited; every self-serve store gets one at signup."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    current_period_end = models.DateTimeField(null=True, blank=True,
        help_text="Akhir masa aktif paket berbayar. Kosong untuk paket gratis.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self): return f"{self.store} · {self.plan}"


class SubscriptionInvoice(models.Model):
    STATUS = [("pending", "Menunggu pembayaran"), ("paid", "Lunas"), ("failed", "Gagal"),
        ("cancelled", "Dibatalkan")]
    PERIODS = [(1, "1 bulan"), (12, "12 bulan")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="subscription_invoices")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    months = models.PositiveSmallIntegerField(choices=PERIODS, default=1)
    amount = models.DecimalField(max_digits=12, decimal_places=0)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    provider = models.CharField(max_length=40)
    payment_reference = models.CharField(max_length=80, blank=True, db_index=True)
    checkout_url = models.URLField(max_length=500, blank=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0),
            name="subscription_invoice_amount_positive")]

    def __str__(self): return f"{self.store} · {self.plan} · {self.get_status_display()}"
