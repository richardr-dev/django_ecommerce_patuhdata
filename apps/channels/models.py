import json

from django.db import models

from apps.catalog.models import Product, ProductVariant
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


class ChannelOrder(models.Model):
    """Links an imported marketplace order to its Lakuin order; importing twice is a no-op."""
    channel = models.ForeignKey(SalesChannel, on_delete=models.PROTECT, related_name="orders")
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="channel_order")
    external_order_id = models.CharField(max_length=80)
    # Set when the marketplace sold more than Lakuin had available: someone must act.
    attention = models.CharField(max_length=300, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["channel", "external_order_id"],
            name="unique_channel_order")]
