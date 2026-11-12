import json

from django.db import models

from apps.catalog.models import Bundle, Product, ProductVariant
from apps.integrations.secrets import decrypt, encrypt
from apps.orders.models import Order
from apps.shops.models import Store

from .adapters import PLATFORMS


class SalesChannel(models.Model):
    """One marketplace shop of a seller, e.g. their Shopee store. Lakuin's stock is the single
    source of truth: orders flow in from the channel, stock flows out to it."""
    CONNECTIONS = [("csv", "Impor CSV"), ("api", "API marketplace")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="sales_channels")
    platform = models.CharField(max_length=20, choices=[(code, p.label) for code, p in PLATFORMS.items()])
    name = models.CharField(max_length=120, help_text="Nama toko di marketplace")
    connection = models.CharField(max_length=10, choices=CONNECTIONS, default="csv")
    external_shop_id = models.CharField(max_length=80, blank=True)
    encrypted_credentials = models.TextField(blank=True)  # JSON: tokens from the marketplace OAuth
    is_active = models.BooleanField(default=True)
    last_order_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["platform", "name"]
        constraints = [models.UniqueConstraint(fields=["store", "platform", "name"],
            name="unique_store_channel_name")]

    def __str__(self):
        return f"{self.get_platform_display()} · {self.name}"

    @property
    def credentials(self):
        return json.loads(decrypt(self.encrypted_credentials) or "{}")

    @credentials.setter
    def credentials(self, value):
        self.encrypted_credentials = encrypt(json.dumps(value)) if value else ""


class ChannelListing(models.Model):
    """A Lakuin product (or variant) as listed on one channel, matched by SKU."""
    channel = models.ForeignKey(SalesChannel, on_delete=models.CASCADE, related_name="listings")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="channel_listings")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True,
        related_name="channel_listings")
    sku = models.CharField(max_length=80)
    external_item_id = models.CharField(max_length=80, blank=True)
    external_sku_id = models.CharField(max_length=80, blank=True)
    # What the channel was last told, so the worker pushes only real changes.
    last_pushed_stock = models.IntegerField(null=True, blank=True)
    last_pushed_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["sku"]
        constraints = [models.UniqueConstraint(fields=["channel", "sku"], name="unique_channel_sku")]

    @property
    def available_stock(self):
        return self.variant.available_stock if self.variant_id else self.product.available_stock


class ChannelBuyer(models.Model):
    """Buyer profile built from marketplace imports — one record per buyer per channel.

    Shopee buyers are identified by username since phone/name are masked.
    Records are upserted on every import so order_count and totals stay current.
    """
    PLATFORMS = [
        ("shopee", "Shopee"),
        ("tokopedia", "Tokopedia"),
        ("tiktok", "TikTok Shop"),
        ("lazada", "Lazada"),
        ("manual", "Manual"),
        ("other", "Lainnya"),
    ]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="channel_buyers")
    platform = models.CharField(max_length=20, choices=PLATFORMS, db_index=True)
    # Shopee: username. Tokped: buyer_id. Manual: phone number.
    external_id = models.CharField(max_length=160, db_index=True)
    name = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    city = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=100, blank=True)
    last_address = models.TextField(blank=True)
    order_count = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    is_member = models.BooleanField(default=False)
    loyalty_points = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
        constraints = [
            models.UniqueConstraint(fields=["store", "platform", "external_id"],
                name="unique_channel_buyer")
        ]

    def __str__(self):
        return f"{self.name or self.external_id} ({self.get_platform_display()})"


class ImportBatch(models.Model):
    """One file upload — groups all orders created from that import.
    Acts as the sales invoice record for marketplace order batches."""
    import uuid as _uuid

    CAT_REGULAR = "regular"
    CAT_FLASH_SALE = "flash_sale"
    CAT_HARBOLNAS = "harbolnas"
    CAT_WHOLESALE = "wholesale"
    CAT_CAMPAIGN = "campaign"
    CAT_CONSIGNMENT = "consignment"
    CAT_CHOICES = [
        (CAT_REGULAR, "Penjualan Reguler"),
        (CAT_FLASH_SALE, "Flash Sale"),
        (CAT_HARBOLNAS, "Harbolnas / Hari Belanja"),
        (CAT_WHOLESALE, "Grosir / Wholesale"),
        (CAT_CAMPAIGN, "Promo / Kampanye"),
        (CAT_CONSIGNMENT, "Konsinyasi"),
    ]

    id = models.UUIDField(primary_key=True, default=_uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="import_batches")
    channel = models.ForeignKey(SalesChannel, on_delete=models.PROTECT,
        related_name="import_batches")
    imported_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="import_batches")
    file_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(
        max_length=20, choices=CAT_CHOICES, default=CAT_REGULAR,
        help_text="Jenis penjualan untuk pengelompokan invoice.")
    is_soft = models.BooleanField(default=False,
        help_text="True when imported without stock deduction (historical mode).")
    order_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)  # existing orders whose status moved
    cancelled_count = models.PositiveIntegerField(default=0)  # cancelled or returned orders
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.channel} · {self.imported_at:%d %b %Y %H:%M} · {self.order_count} pesanan"

    @property
    def platform_label(self):
        from .adapters import PLATFORMS
        return PLATFORMS.get(self.channel.platform, type("P", (), {"label": self.channel.platform})()).label


class ChannelOrder(models.Model):
    """Links an imported marketplace order to its Lakuin order; importing twice is a no-op."""
    channel = models.ForeignKey(SalesChannel, on_delete=models.PROTECT, related_name="orders")
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="channel_order")
    external_order_id = models.CharField(max_length=80)
    batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="channel_orders")
    buyer = models.ForeignKey(ChannelBuyer, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="channel_orders")
    # Set when the marketplace sold more than Lakuin had available: someone must act.
    attention = models.CharField(max_length=300, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["channel", "external_order_id"],
            name="unique_channel_order")]


class ChannelSku(models.Model):
    """What a marketplace calls an item, mapped to the store's own item or bundle. Needed when
    the SKU typed in Shopee or TikTok differs from the master SKU."""
    channel = models.ForeignKey(SalesChannel, on_delete=models.CASCADE, related_name="sku_map")
    external_sku = models.CharField(max_length=120)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True,
        related_name="+")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True,
        related_name="+")
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, null=True, blank=True,
        related_name="+")

    class Meta:
        ordering = ["external_sku"]
        constraints = [
            models.UniqueConstraint(fields=["channel", "external_sku"], name="unique_channel_sku_map"),
            models.CheckConstraint(name="channel_sku_has_one_target", condition=(
                models.Q(product__isnull=False, bundle__isnull=True)
                | models.Q(product__isnull=True, bundle__isnull=False))),
        ]

    @property
    def target_label(self):
        if self.bundle_id:
            return f"Paket {self.bundle.sku} · {self.bundle.name}"
        item = self.variant if self.variant_id else self.product
        return f"{item.sku} · {self.product.name}"


class UnmappedSku(models.Model):
    """A marketplace SKU that matched nothing in the catalog: the review queue. In strict mode
    its order was refused; in historical mode a placeholder product was created for it."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="unmapped_skus")
    channel = models.ForeignKey(SalesChannel, on_delete=models.CASCADE, related_name="unmapped_skus")
    sku = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=200, blank=True)
    placeholder = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    times_seen = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["resolved", "-last_seen"]
        constraints = [models.UniqueConstraint(fields=["channel", "sku", "name"],
            name="unique_unmapped_sku")]


class ChannelOrderIncome(models.Model):
    """What the marketplace actually paid out for one order, and what it kept: commission,
    service and admin fees, shipping, vouchers and affiliate commission. Built from the
    marketplace's income report, so each order has a real net revenue and margin."""
    FEE_FIELDS = ["commission", "service_fee", "admin_fee", "shipping_fee", "voucher",
        "affiliate", "other_fee"]
    channel_order = models.OneToOneField(ChannelOrder, on_delete=models.CASCADE,
        related_name="income")
    commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    service_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    admin_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    voucher = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    affiliate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Whatever the marketplace deducted that its report does not itemise.
    other_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_released = models.DecimalField(max_digits=14, decimal_places=2)
    released_on = models.DateField(null=True, blank=True)
    payout_reference = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def total_fees(self):
        return sum((getattr(self, name) for name in self.FEE_FIELDS), 0)
