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
    included_outlets = models.PositiveIntegerField(null=True, blank=True, default=1,
        help_text="Outlet yang sudah termasuk harga paket. Kosongkan untuk tanpa batas.")
    extra_outlet_price = models.DecimalField(max_digits=12, decimal_places=0, null=True,
        blank=True, help_text="Harga per outlet tambahan per bulan. Kosong: tidak bisa menambah "
            "outlet (perlu upgrade).")
    max_channels = models.PositiveIntegerField(null=True, blank=True,
        help_text="Toko marketplace yang dapat dihubungkan. Kosongkan untuk tanpa batas.")
    allow_payout_reconciliation = models.BooleanField(default=False,
        help_text="Catat dan cocokkan pencairan dana gateway & marketplace.")
    contact_sales = models.BooleanField(default=False,
        help_text="Paket khusus: penjual menghubungi tim sales, tidak bisa dibeli sendiri. "
            "Pengelola platform menetapkannya ke toko lewat admin.")
    features = models.TextField(blank=True, help_text="Satu keunggulan per baris, tampil di "
        "halaman harga.")
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

    @property
    def feature_list(self): return [line.strip() for line in self.features.splitlines() if line.strip()]

    def price_for(self, months):
        return self.price_yearly if months == 12 else self.price_monthly


class Subscription(models.Model):
    """A store's plan. Stores without one were created by the platform operator and are
    not limited; every self-serve store gets one at signup."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    current_period_end = models.DateTimeField(null=True, blank=True,
        help_text="Akhir masa aktif paket berbayar. Kosong untuk paket gratis.")
    # Before each period ends Lakuin bills the next one and reminds the seller to pay it.
    auto_renew = models.BooleanField(default=True)
    # Outlets bought on top of those the plan includes; renewals bill them too.
    extra_outlets = models.PositiveIntegerField(default=0)
    renew_months = models.PositiveSmallIntegerField(default=1, choices=[(1, "Bulanan"),
        (12, "Tahunan")])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self): return f"{self.store} · {self.plan}"


class SubscriptionInvoice(models.Model):
    STATUS = [("pending", "Menunggu pembayaran"), ("paid", "Lunas"), ("failed", "Gagal"),
        ("cancelled", "Dibatalkan")]
    PERIODS = [(1, "1 bulan"), (12, "12 bulan")]
    KINDS = [("checkout", "Dibeli dari menu Langganan"), ("renewal", "Perpanjangan otomatis"),
        ("outlets", "Tambah outlet")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=10, choices=KINDS, default="checkout")
    # Renewals: the period end they continue from, and how many reminders went out.
    due_at = models.DateTimeField(null=True, blank=True)
    reminders_sent = models.PositiveSmallIntegerField(default=0)
    # Extra outlets this invoice pays for: all of them for a period, or those being added.
    extra_outlets = models.PositiveIntegerField(default=0)
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
            name="subscription_invoice_amount_positive"),
            models.UniqueConstraint(fields=["store", "due_at"],
                condition=models.Q(kind="renewal"), name="one_renewal_per_period")]

    def __str__(self): return f"{self.store} · {self.plan} · {self.get_status_display()}"


class SubscriptionCheckout(models.Model):
    """One payment page opened for an invoice. A DOKU page expires within the hour, so a renewal
    invoice can get several; every reference is kept so a late payment on an older page is
    still matched to its invoice."""
    invoice = models.ForeignKey(SubscriptionInvoice, on_delete=models.CASCADE,
        related_name="checkouts")
    reference = models.CharField(max_length=80, unique=True)
    checkout_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
