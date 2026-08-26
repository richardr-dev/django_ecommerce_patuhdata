import uuid
from decimal import Decimal

from django.db import models

from apps.catalog.models import Product, ProductVariant
from apps.shops.models import Store


class Order(models.Model):
    FULFILLMENT_METHODS = [
        ("merchant_pickup", "Kurir pickup dari toko"),
        ("merchant_dropoff", "Toko antar ke drop point"),
        ("third_party", "Gudang 3PL"),
    ]
    TRACKING_STATUSES = [
        ("not_ready", "Belum siap dikirim"),
        ("ready_to_track", "Resi tersimpan"),
        ("awaiting_pickup", "Menunggu pickup/drop-off"),
        ("in_transit", "Dalam perjalanan"),
        ("out_for_delivery", "Sedang diantar"),
        ("delivered", "Terkirim"),
        ("returned", "Dikembalikan"),
        ("exception", "Kendala pengiriman"),
        ("unknown", "Status belum dikenali"),
    ]
    STATUS = [("pending", "Pending Payment"), ("payment_failed", "Payment Failed"),
        ("paid", "Paid"), ("confirmed", "Confirmed"), ("allocated", "Allocated"),
        ("picking", "Picking"), ("packed", "Packed"), ("shipped", "Shipped"),
        ("delivered", "Delivered"), ("completed", "Completed"), ("cancelled", "Cancelled"),
        ("refund_requested", "Refund Requested"), ("partially_refunded", "Partially Refunded"),
        ("refunded", "Refunded"), ("returned", "Returned")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    access_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="orders")
    customer_user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customer_orders")
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
    courier_code = models.CharField(max_length=40, blank=True)
    fulfillment_method = models.CharField(max_length=30, choices=FULFILLMENT_METHODS,
        default="merchant_pickup")
    tracking_number = models.CharField(max_length=100, blank=True)
    tracking_status = models.CharField(max_length=30, choices=TRACKING_STATUSES,
        default="not_ready")
    tracking_status_detail = models.CharField(max_length=300, blank=True)
    tracking_last_checked_at = models.DateTimeField(null=True, blank=True)
    tracking_error = models.CharField(max_length=300, blank=True)
    external_shipping_id = models.CharField(max_length=100, blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    coupon_code = models.CharField(max_length=50, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(subtotal__gte=0), name="order_subtotal_nonnegative"),
            models.CheckConstraint(condition=models.Q(discount__gte=0), name="order_discount_nonnegative"),
            models.CheckConstraint(condition=models.Q(shipping_cost__gte=0), name="order_shipping_nonnegative"),
            models.CheckConstraint(condition=models.Q(tax__gte=0), name="order_tax_nonnegative"),
            models.CheckConstraint(condition=models.Q(total__gte=0), name="order_total_nonnegative"),
        ]

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

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="order_item_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name="order_item_price_nonnegative"),
            models.CheckConstraint(condition=models.Q(line_total__gte=0), name="order_item_total_nonnegative"),
        ]

class InventoryAllocation(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="allocations")
    inventory_level = models.ForeignKey("catalog.InventoryLevel", on_delete=models.PROTECT, related_name="allocations")
    quantity = models.PositiveIntegerField()
    committed_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["order_item", "inventory_level"], name="unique_item_inventory_allocation")]


class InventoryMovement(models.Model):
    KIND = [("reserve", "Reserve"), ("release", "Release"), ("commit", "Commit"),
        ("adjust", "Adjust"), ("return", "Return")]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="inventory_movements")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="inventory_movements",
        null=True, blank=True)
    inventory_level = models.ForeignKey("catalog.InventoryLevel", on_delete=models.PROTECT,
        related_name="movements", null=True, blank=True)
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT,
        related_name="inventory_movements", null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KIND)
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=200)
    actor = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    previous_on_hand = models.PositiveIntegerField()
    new_on_hand = models.PositiveIntegerField()
    previous_reserved = models.PositiveIntegerField()
    new_reserved = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(inventory_level__isnull=False) |
            models.Q(variant__isnull=False)), name="movement_has_stock_target")]

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
    provider_payment_id = models.CharField(max_length=160, blank=True)
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
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_id", "kind"],
                name="unique_provider_transaction"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
        ]


class Refund(models.Model):
    STATUS = [("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")]
    REASONS = [("REQUESTED_BY_CUSTOMER", "Requested by customer"), ("CANCELLATION", "Cancellation"),
        ("DUPLICATE", "Duplicate"), ("FRAUDULENT", "Fraudulent"), ("OTHERS", "Other")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(PaymentTransaction, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.CharField(max_length=40, choices=REASONS)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    provider_reference = models.CharField(max_length=160, blank=True)
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="refund_amount_positive")]


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


class ShipmentTrackingEvent(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="tracking_events")
    fingerprint = models.CharField(max_length=64)
    provider = models.CharField(max_length=40, default="rajaongkir")
    status = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=500)
    location = models.CharField(max_length=160, blank=True)
    occurred_at = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        constraints = [models.UniqueConstraint(fields=["order", "fingerprint"],
            name="unique_order_tracking_event")]


class ReturnRequest(models.Model):
    STATUS = [("requested", "Requested"), ("approved", "Approved"), ("rejected", "Rejected"), ("refunded", "Refunded")]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="return_requests")
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS, default="requested")
    created_at = models.DateTimeField(auto_now_add=True)
