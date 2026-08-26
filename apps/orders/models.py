import uuid
from decimal import Decimal

from django.db import models

from apps.catalog.models import Product, ProductVariant
from apps.shops.models import Store


class Order(models.Model):
    STATUS = [("pending", "Pending Payment"), ("payment_failed", "Payment Failed"),
        ("paid", "Paid"), ("confirmed", "Confirmed"), ("allocated", "Allocated"),
        ("picking", "Picking"), ("packed", "Packed"), ("shipped", "Shipped"),
        ("delivered", "Delivered"), ("completed", "Completed"), ("cancelled", "Cancelled"),
        ("refund_requested", "Refund Requested"), ("partially_refunded", "Partially Refunded"),
        ("refunded", "Refunded"), ("returned", "Returned")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    customer_name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    shipping_address = models.TextField()
    address_label = models.CharField(max_length=40, blank=True)
    province = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    village = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    destination_area_id = models.CharField(max_length=100, blank=True)
    delivery_notes = models.CharField(max_length=300, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    payment_provider = models.CharField(max_length=40, default="demo")
    payment_method = models.CharField(max_length=40, default="qris")
    payment_reference = models.CharField(max_length=160, blank=True)
    payment_checkout_url = models.URLField(blank=True)
    payment_expires_at = models.DateTimeField(null=True, blank=True)
    shipping_provider = models.CharField(max_length=40, blank=True)
    shipping_service = models.CharField(max_length=80, blank=True)
    shipping_eta = models.CharField(max_length=80, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    external_shipping_id = models.CharField(max_length=100, blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    coupon_code = models.CharField(max_length=50, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True)
    name = models.CharField(max_length=160)
    sku = models.CharField(max_length=80)
    variant_name = models.CharField(max_length=160, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    quantity = models.PositiveIntegerField()
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

class InventoryAllocation(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="allocations")
    inventory_level = models.ForeignKey("catalog.InventoryLevel", on_delete=models.PROTECT, related_name="allocations")
    quantity = models.PositiveIntegerField()
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["order_item", "inventory_level"], name="unique_item_inventory_allocation")]

class PaymentEvent(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    provider = models.CharField(max_length=40)
    external_id = models.CharField(max_length=160)
    event_type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "external_id"], name="unique_payment_event")]


class PaymentTransaction(models.Model):
    KIND = [("payment", "Payment"), ("refund", "Refund")]
    STATUS = [("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")]
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payment_transactions")
    provider = models.CharField(max_length=40)
    external_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=KIND, default="payment")
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="IDR")
    reconciled = models.BooleanField(default=False)
    mismatch_reason = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "external_id", "kind"],
            name="unique_provider_transaction")]


class ShippingMethod(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="shipping_methods")
    code = models.SlugField()
    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=40, default="manual")
    service = models.CharField(max_length=80, blank=True)
    flat_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    free_above = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    eta = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_store_shipping_code")]

    def quote(self, subtotal):
        if self.free_above is not None and subtotal >= self.free_above:
            return Decimal(0)
        return self.flat_rate

    def __str__(self):
        return f"{self.name} ({self.store})"


class Coupon(models.Model):
    KIND = [("fixed", "Nominal"), ("percent", "Persentase"), ("free_shipping", "Gratis ongkir")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="coupons")
    code = models.CharField(max_length=50)
    kind = models.CharField(max_length=20, choices=KIND)
    value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    minimum_spend = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_store_coupon_code")]


class OrderStatusEvent(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_events")
    status = models.CharField(max_length=20, choices=Order.STATUS)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class ReturnRequest(models.Model):
    STATUS = [("requested", "Requested"), ("approved", "Approved"), ("rejected", "Rejected"), ("refunded", "Refunded")]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="return_requests")
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS, default="requested")
    created_at = models.DateTimeField(auto_now_add=True)
