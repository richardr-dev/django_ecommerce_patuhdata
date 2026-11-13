import uuid
from decimal import Decimal

from django.db import models, transaction

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
    STATUS = [("pending", "Menunggu pembayaran"), ("payment_failed", "Pembayaran gagal"),
        ("paid", "Dibayar"), ("confirmed", "Dikonfirmasi"), ("allocated", "Stok dialokasikan"),
        ("picking", "Diambil dari rak"), ("packed", "Dikemas"), ("shipped", "Dikirim"),
        ("delivered", "Diterima pembeli"), ("completed", "Selesai"), ("cancelled", "Dibatalkan"),
        ("refund_requested", "Refund diajukan"), ("partially_refunded", "Refund sebagian"),
        ("refunded", "Direfund"), ("returned", "Diretur")]
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
    # True when the buyer paid the seller's own gateway or bank account, not the platform's.
    paid_to_store_account = models.BooleanField(default=False)
    shipping_provider = models.CharField(max_length=40, blank=True)
    shipping_service = models.CharField(max_length=80, blank=True)
    shipping_eta = models.CharField(max_length=80, blank=True)
    courier_code = models.CharField(max_length=40, blank=True)
    courier_service_code = models.CharField(max_length=40, blank=True)
    fulfillment_method = models.CharField(max_length=30, choices=FULFILLMENT_METHODS,
        default="merchant_pickup")
    tracking_number = models.CharField(max_length=100, blank=True)
    tracking_status = models.CharField(max_length=30, choices=TRACKING_STATUSES,
        default="not_ready")
    tracking_status_detail = models.CharField(max_length=300, blank=True)
    tracking_last_checked_at = models.DateTimeField(null=True, blank=True)
    tracking_error = models.CharField(max_length=300, blank=True)
    external_shipping_id = models.CharField(max_length=100, blank=True)
    label_printed_at = models.DateTimeField(null=True, blank=True)
    # The day (and slot) the shopper chose for delivery, when the store schedules deliveries or
    # the cart holds pre-order products.
    delivery_date = models.DateField(null=True, blank=True, db_index=True)
    delivery_slot = models.ForeignKey("DeliverySlot", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="orders")
    delivery_slot_label = models.CharField(max_length=40, blank=True)
    # Set when the shopper collects the order at one of the store's outlets.
    pickup_outlet = models.ForeignKey("catalog.Warehouse", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="pickup_orders")
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    loyalty_discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    loyalty_points_redeemed = models.PositiveIntegerField(default=0)
    loyalty_points_earned = models.PositiveIntegerField(default=0)
    coupon_code = models.CharField(max_length=50, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    # Store credit from a gift card, already included in `discount`.
    gift_card = models.ForeignKey("orders.GiftCard", on_delete=models.PROTECT, null=True,
        blank=True, related_name="orders")
    gift_card_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # "manual": created by the seller in the back office (a chat order) and paid through a link.
    source = models.CharField(max_length=10, default="storefront",
        choices=[("storefront", "Toko online"), ("manual", "Dibuat penjual")])
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_orders")
    # The seller's campaign short link (lakuin.store/s/<code>) the shopper last came through.
    campaign_link = models.ForeignKey("shops.CampaignLink", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="orders")
    created_at = models.DateTimeField(auto_now_add=True)
    # ── Commerce channel & invoice ────────────────────────────────────────────
    # Channels represent WHERE the sale originated.
    # AB Shop is a reseller/partner: orders tagged `abshop` are B2B sell-in orders
    # where the store invoices AB Shop directly. AB Shop then resells on their own
    # Tokopedia/Shopee accounts — that downstream activity is recorded separately
    # as SELL_THROUGH (informational only, no store revenue movement).
    CHANNELS = [
        # ── Direct / owned channels ───────────────────────────────────────────
        ("web", "Toko Online"),
        ("shopee", "Shopee"),
        ("tokopedia", "Tokopedia / TikTok Shop"),
        ("tiktok_shop", "TikTok Shop"),
        ("lazada", "Lazada"),
        ("pos", "POS Retail"),
        ("manual", "Manual / Chat"),
        ("tv", "TV Shopping"),
        # ── Reseller / partner channel ────────────────────────────────────────
        ("abshop", "AB Shop (Reseller)"),
    ]
    ORDER_TYPES = [
        ("direct", "Penjualan Langsung"),      # B2C: store → customer
        ("reseller_sell_in", "Jual ke Reseller"),  # B2B: store → AB Shop (creates invoice & stock handover)
        ("reseller_sell_through", "Penjualan Reseller"),  # Downstream: AB Shop → end customer (informational)
    ]
    ABSHOP_BILLING_STATUSES = [
        ("unbilled", "Belum Ditagih"),
        ("billed", "Sudah Ditagih"),
        ("paid", "Sudah Dibayar"),
    ]
    # Sub-channel for AB Shop orders: which platform AB Shop sells through to end customers
    AB_SHOP_SOURCES = [
        ("shopee", "Shopee AB Shop"),
        ("tokopedia", "Tokopedia AB Shop"),
        ("tv", "TV Shopping"),
    ]
    # Sequential, gap-free invoice number per store, e.g. INV-2025-00042
    invoice_number = models.CharField(max_length=80, blank=True, db_index=True)
    channel = models.CharField(max_length=40, choices=CHANNELS, default="web", db_index=True)
    order_type = models.CharField(max_length=30, choices=ORDER_TYPES, default="direct", db_index=True)
    # Marketplace order ID when imported from Shopee/Tokopedia
    external_order_id = models.CharField(max_length=100, blank=True, db_index=True)
    # Raw status string from marketplace (e.g. "Selesai", "Dikirim")
    marketplace_status = models.CharField(max_length=60, blank=True)
    # Tracks whether this AB Shop reseller sell-in has been invoiced to them
    abshop_billing_status = models.CharField(max_length=30, choices=ABSHOP_BILLING_STATUSES,
        default="unbilled")
    # For AB Shop orders: which platform AB Shop used to sell to end customers
    ab_shop_source = models.CharField(max_length=20, choices=AB_SHOP_SOURCES, blank=True,
        db_index=True)
    # Direct link to the consignment partner for Mitra-channel invoices
    consignment_partner = models.ForeignKey(
        "settlements.ConsignmentPartner", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="orders")
    # CRM link — set by apps.shops.crm.sync_customer_from_order; null for anonymous orders
    customer = models.ForeignKey(
        "shops.Customer", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="orders",
        help_text="Linked CRM customer. Set by sync service."
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(subtotal__gte=0), name="order_subtotal_nonnegative"),
            models.CheckConstraint(condition=models.Q(discount__gte=0), name="order_discount_nonnegative"),
            models.CheckConstraint(condition=models.Q(loyalty_discount__gte=0),
                name="order_loyalty_discount_nonnegative"),
            models.CheckConstraint(condition=models.Q(shipping_cost__gte=0), name="order_shipping_nonnegative"),
            models.CheckConstraint(condition=models.Q(tax__gte=0), name="order_tax_nonnegative"),
            models.CheckConstraint(condition=models.Q(total__gte=0), name="order_total_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        """Auto-generate a gap-free invoice number on first save."""
        if not self.invoice_number and self.id:
            year = self.created_at.year if self.created_at else __import__("datetime").date.today().year
            last = (Order.objects.filter(store=self.store,
                invoice_number__startswith=f"INV-{year}-")
                .order_by("-invoice_number").values_list("invoice_number", flat=True).first())
            seq = 1
            if last:
                try:
                    seq = int(last.split("-")[-1]) + 1
                except (ValueError, IndexError):
                    seq = 1
            self.invoice_number = f"INV-{year}-{seq:05d}"
        super().save(*args, **kwargs)

    def get_channel_badge(self):
        """Return a short label suitable for a coloured badge."""
        return dict(self.CHANNELS).get(self.channel, self.channel)

    @property
    def is_abshop(self):
        """True when this order is a B2B sell-in to AB Shop reseller."""
        return self.channel == "abshop"

    @property
    def is_sell_through(self):
        """True for informational downstream AB Shop sales (no store revenue)."""
        return self.order_type == "reseller_sell_through"

    @property
    def full_address(self):
        """Street or building, then kelurahan, kecamatan, city, province and postcode: what a
        courier needs. Parts the shopper already typed in the street are not repeated."""
        street = self.shipping_address.strip()
        parts, seen = [street] if street else [], street.lower()
        for part in (self.village, self.district, self.city, self.province):
            part = (part or "").strip()
            if part and part.lower() not in seen:  # e.g. kelurahan and kecamatan both "Pesanggrahan"
                parts.append(part)
                seen += f" {part.lower()}"
        address = ", ".join(parts)
        if self.postal_code and self.postal_code not in street:
            address = f"{address} {self.postal_code}".strip()
        return address


class MobileCartItem(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="mobile_cart_items")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="mobile_cart_items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="mobile_cart_items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True,
        related_name="mobile_cart_items")
    quantity = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="mobile_cart_quantity_positive"),
            models.UniqueConstraint(fields=["store", "user", "product"],
                condition=models.Q(variant__isnull=True), name="unique_mobile_cart_product"),
            models.UniqueConstraint(fields=["store", "user", "variant"],
                condition=models.Q(variant__isnull=False), name="unique_mobile_cart_variant"),
        ]


class MobileAccessToken(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="mobile_access_tokens")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="mobile_access_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["store", "user", "expires_at"],
            name="orders_mobile_store_u_idx")]


class LoyaltyAccount(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="loyalty_accounts")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="loyalty_accounts")
    balance = models.IntegerField(default=0)
    lifetime_earned = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "user"],
            name="unique_store_user_loyalty")]


class LoyaltyTransaction(models.Model):
    KINDS = [("earn", "Earn"), ("redeem", "Redeem"), ("restore", "Restore"),
        ("reversal", "Earn reversal"), ("adjust", "Manual adjustment")]
    account = models.ForeignKey(LoyaltyAccount, on_delete=models.CASCADE, related_name="transactions")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="loyalty_transactions",
        null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    points = models.IntegerField(help_text="Positive credits the balance; negative debits it.")
    balance_after = models.IntegerField()
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(points=0), name="loyalty_points_nonzero"),
            models.UniqueConstraint(fields=["order", "kind"], condition=models.Q(order__isnull=False),
                name="unique_order_loyalty_kind"),
        ]


class CommerceSession(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="commerce_sessions")
    visitor_id = models.UUIDField(default=uuid.uuid4)
    user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="commerce_sessions")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    last_path = models.CharField(max_length=300, blank=True)
    product_view_count = models.PositiveIntegerField(default=0)
    checkout_started_count = models.PositiveIntegerField(default=0)
    cart_item_count = models.PositiveIntegerField(default=0)
    cart_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cart_snapshot = models.JSONField(default=list)
    converted_at = models.DateTimeField(null=True, blank=True)
    recovery_token = models.UUIDField(default=uuid.uuid4, unique=True)
    recovery_email = models.EmailField(blank=True)
    recovery_consent_at = models.DateTimeField(null=True, blank=True)
    recovery_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "visitor_id"],
            name="unique_store_visitor_session")]
        indexes = [models.Index(fields=["store", "last_seen_at"], name="orders_comm_store_i_d65ad6_idx"),
            models.Index(fields=["store", "converted_at"], name="orders_comm_store_i_4944ac_idx")]


class JourneyEvent(models.Model):
    KINDS = [("home_view", "Home view"), ("search", "Search"),
        ("product_view", "Product view"), ("cart_add", "Cart add"),
        ("cart_update", "Cart update"), ("checkout_started", "Checkout started"),
        ("converted", "Converted")]
    session = models.ForeignKey(CommerceSession, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30, choices=KINDS)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["session", "kind", "created_at"], name="orders_jour_session_73a50d_idx")]


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True)
    flash_sale_offer = models.ForeignKey("catalog.FlashSaleProduct", on_delete=models.PROTECT,
        related_name="order_items", null=True, blank=True)
    name = models.CharField(max_length=160)
    sku = models.CharField(max_length=80)
    variant_name = models.CharField(max_length=160, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    quantity = models.PositiveIntegerField()
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    # Made to order: counted against the product's daily quota, never against warehouse stock.
    is_preorder = models.BooleanField(default=False)
    # The shopper's note for this item, e.g. the writing on a cake.
    note = models.CharField(max_length=200, blank=True)
    # The bundle SKU this line was sold as, when it is one component of a bundle.
    bundle_sku = models.CharField(max_length=80, blank=True)
    # Cost per unit copied when the sale is booked, so later cost changes keep past profit.
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # ABShop reseller price charged to AB Shop; different from regular retail unit_price.
    ab_shop_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    @property
    def effective_ab_shop_price(self):
        """The price used when billing the mitra of this order for this item: a price set on the
        line, else the mitra's own price list, else the general mitra price, else the line price."""
        if self.ab_shop_price is not None:
            return self.ab_shop_price
        from apps.settlements.pricing import partner_price
        price = partner_price(self.order.consignment_partner, self.product, self.variant)
        if price is None and self.order.consignment_partner_id is None:
            # An order with no mitra recorded keeps the older general mitra price.
            if self.variant and self.variant.ab_shop_price is not None:
                return self.variant.ab_shop_price
            if self.product and self.product.ab_shop_price is not None:
                return self.product.ab_shop_price
        return price if price is not None else self.unit_price

    @property
    def ab_shop_line_total(self):
        return self.effective_ab_shop_price * self.quantity

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


class MovementImmutable(Exception):
    pass


class MovementQuerySet(models.QuerySet):
    """The movement ledger is append-only: a mistake is corrected by a new movement."""

    def update(self, **kwargs):
        raise MovementImmutable("Pergerakan stok tidak boleh diubah. Buat pergerakan koreksi.")

    def delete(self):
        raise MovementImmutable("Pergerakan stok tidak boleh dihapus. Buat pergerakan koreksi.")


class InventoryMovement(models.Model):
    objects = MovementQuerySet.as_manager()
    KIND = [("reserve", "Reserve"), ("release", "Release"), ("commit", "Commit"),
        ("adjust", "Adjust"), ("return", "Return"), ("receipt", "Barang masuk"),
        ("transfer_in", "Transfer masuk"), ("transfer_out", "Transfer keluar"),
        ("consign_out", "Kirim ke mitra"), ("consign_in", "Kembali dari mitra"),
        ("opname", "Stok opname"), ("deficit", "Jual melebihi stok"), ("purchase_return", "Retur ke supplier"),
        ("deficit_cover", "Tutup stok minus")]
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
    # The document behind a movement that no order caused (goods receipt, transfer, mitra...).
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(inventory_level__isnull=False) |
            models.Q(variant__isnull=False)), name="movement_has_stock_target")]

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise MovementImmutable("Pergerakan stok tidak boleh diubah. Buat pergerakan koreksi.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise MovementImmutable("Pergerakan stok tidak boleh dihapus. Buat pergerakan koreksi.")

class StockDeficit(models.Model):
    """Stok minus: units sold beyond what the shelf held (only when the store allows it). The shelf
    figure itself never goes below zero; the shortfall lives here, valued at the cost known at the
    sale (provisional). The next goods receipt covers it first and corrects the cost of goods sold."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="stock_deficits")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_deficits")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="stock_deficits")
    inventory_level = models.ForeignKey("catalog.InventoryLevel", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    covered = models.PositiveIntegerField(default=0)
    provisional_cost = models.DecimalField(max_digits=14, decimal_places=2)
    released_at = models.DateTimeField(null=True, blank=True)  # the sale was cancelled or edited
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(covered__lte=models.F("quantity")),
            name="deficit_covered_lte_quantity")]

    @property
    def open(self):
        return 0 if self.released_at else self.quantity - self.covered


class StockDeficitCover(models.Model):
    """Units of a goods receipt that went to cover a deficit, at the receipt's real cost."""
    deficit = models.ForeignKey(StockDeficit, on_delete=models.PROTECT, related_name="covers")
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2)
    covered_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def adjustment(self):
        """What the cost of goods sold was short by (negative: it was too high)."""
        return self.quantity * (self.unit_cost - self.deficit.provisional_cost)


class StockRequest(models.Model):
    """Permintaan stok: an outlet (or any warehouse) asks another warehouse of the same company for goods.
    requested → approved → sent → received. Goods leave the source warehouse when sent and are *in transit*
    (still the company's Persediaan, on no shelf) until the receiver counts them in. Fewer received than sent
    is a loss in transit, booked as Selisih persediaan at cost. Inside one company this never touches the
    books otherwise: no sale, no HPP, same average cost."""
    REQUESTED, APPROVED, SENT, RECEIVED, REJECTED, CANCELLED = "requested", "approved", "sent", "received", "rejected", "cancelled"
    STATUS_CHOICES = [(REQUESTED, "Diminta"), (APPROVED, "Disetujui"), (SENT, "Dikirim"), (RECEIVED, "Diterima"),
        (REJECTED, "Ditolak"), (CANCELLED, "Dibatalkan")]
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="stock_requests")
    number = models.CharField(max_length=40)
    from_warehouse = models.ForeignKey("catalog.Warehouse", on_delete=models.PROTECT, related_name="requests_out")
    to_warehouse = models.ForeignKey("catalog.Warehouse", on_delete=models.PROTECT, related_name="requests_in")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=REQUESTED)
    notes = models.CharField(max_length=300, blank=True)
    requested_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sent_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    received_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["store", "number"], name="unique_stock_request_number"),
            models.CheckConstraint(condition=~models.Q(from_warehouse=models.F("to_warehouse")),
                name="stock_request_two_warehouses")]


class StockRequestLine(models.Model):
    request = models.ForeignKey(StockRequest, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    sent_quantity = models.PositiveIntegerField(default=0)
    received_quantity = models.PositiveIntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # frozen when sent

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=models.Q(sent_quantity__lte=models.F("quantity"),
            received_quantity__lte=models.F("sent_quantity")), name="stock_request_quantities_in_order")]

    @property
    def in_transit(self):
        return self.sent_quantity - self.received_quantity


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
    METHODS = [("gateway", "Payment gateway"), ("manual_transfer", "Manual bank transfer")]
    REASONS = [("REQUESTED_BY_CUSTOMER", "Requested by customer"), ("CANCELLATION", "Cancellation"),
        ("DUPLICATE", "Duplicate"), ("FRAUDULENT", "Fraudulent"), ("OTHERS", "Other")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(PaymentTransaction, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.CharField(max_length=40, choices=REASONS)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    method = models.CharField(max_length=20, choices=METHODS, default="gateway")
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
    per_customer_limit = models.PositiveIntegerField(null=True, blank=True,
        help_text="Maksimal pemakaian per pelanggan (akun atau nomor WhatsApp). Kosongkan jika tanpa batas.")
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
    """Full enterprise return/exchange workflow with photo evidence and tracking."""
    STATUS = [
        ("requested", "Diminta"),
        ("approved", "Disetujui"),
        ("rejected", "Ditolak"),
        ("item_received", "Barang Diterima"),
        ("inspected", "Diperiksa"),
        ("replacement_sent", "Pengganti Dikirim"),
        ("refunded", "Sudah Direfund"),
        ("completed", "Selesai"),
    ]
    TYPES = [
        ("return_refund", "Retur & Refund"),
        ("exchange", "Tukar Ukuran/Barang"),
    ]
    REASONS = [
        ("wrong_size", "Ukuran Tidak Sesuai"),
        ("defective", "Produk Cacat/Rusak"),
        ("wrong_item", "Produk Berbeda"),
        ("not_as_described", "Tidak Sesuai Deskripsi"),
        ("other", "Lainnya"),
    ]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="return_requests")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT,
        related_name="return_requests", null=True, blank=True)
    quantity = models.PositiveIntegerField(default=0,
        help_text="Jumlah yang dikembalikan atau ditukar. 0 berarti semua unit di baris itu.")
    request_type = models.CharField(max_length=20, choices=TYPES, default="return_refund")
    reason = models.CharField(max_length=30, choices=REASONS, default="other")
    reason_detail = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS, default="requested")
    # For exchanges: the variant the customer wants
    original_variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT,
        related_name="return_originals", null=True, blank=True)
    replacement_variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT,
        related_name="return_replacements", null=True, blank=True)
    # Photo evidence (required before processing); FileField avoids Pillow dependency
    photo = models.FileField(upload_to="returns/photos/%Y/%m/", null=True, blank=True)
    # Return shipment tracking
    return_tracking_number = models.CharField(max_length=100, blank=True)
    return_courier = models.CharField(max_length=60, blank=True)
    replacement_tracking_number = models.CharField(max_length=100, blank=True)
    replacement_courier = models.CharField(max_length=60, blank=True)
    # Admin notes
    admin_notes = models.TextField(blank=True)
    # Inspection and settlement: what condition the goods came back in decides whether they go
    # back on the shelf; the credit note (refund) or the stock movements (exchange) are the record.
    CONDITIONS = [("good", "Layak jual kembali"), ("damaged", "Rusak / tidak layak jual")]
    condition = models.CharField(max_length=10, choices=CONDITIONS, blank=True)
    credit_note = models.ForeignKey("tax.CreditNote", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="return_requests")
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Return #{self.pk} – {self.order} [{self.get_status_display()}]"


class CustomerAddress(models.Model):
    """A signed-in customer's saved delivery address. Field names match the checkout form, so a
    saved address can pre-fill it directly."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="customer_addresses")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=40, default="Rumah")
    customer_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)
    province = models.CharField(max_length=80)
    city = models.CharField(max_length=80)
    district = models.CharField(max_length=80)
    village = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=5)
    shipping_address = models.TextField()
    delivery_notes = models.CharField(max_length=200, blank=True)
    destination_area_id = models.CharField(max_length=120, blank=True)
    is_default = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-updated_at"]
        constraints = [models.UniqueConstraint(fields=["store", "user"], condition=models.Q(is_default=True),
            name="one_default_address_per_customer")]

    CHECKOUT_FIELDS = ("customer_name", "phone", "province", "city", "district", "village",
        "postal_code", "shipping_address", "delivery_notes", "destination_area_id")

    def __str__(self):
        return f"{self.label} · {self.city}"

    def make_default(self):
        with transaction.atomic():
            CustomerAddress.objects.filter(store=self.store, user=self.user, is_default=True).exclude(
                id=self.id).update(is_default=False)
            self.is_default = True
            self.save(update_fields=["is_default", "updated_at"])


class DeliverySettings(models.Model):
    """When a store delivers: shoppers pick a date (and a slot) at checkout."""
    WEEKDAYS = [("0", "Senin"), ("1", "Selasa"), ("2", "Rabu"), ("3", "Kamis"), ("4", "Jumat"),
        ("5", "Sabtu"), ("6", "Minggu")]
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="delivery_settings")
    enabled = models.BooleanField(default=False, verbose_name="Pembeli memilih tanggal kirim",
        help_text="Aktifkan bila pembeli perlu memilih tanggal (dan jam) pengiriman, termasuk untuk "
            "produk pre-order. Bila tidak aktif, pesanan dikirim sesuai urutan masuk.")
    min_days = models.PositiveSmallIntegerField(default=1, verbose_name="Pemesanan paling lambat (hari sebelumnya)",
        help_text="0 = bisa dikirim hari ini, 1 = paling cepat besok.")
    max_days = models.PositiveSmallIntegerField(default=14, verbose_name="Bisa dipesan sampai (hari ke depan)")
    closed_weekdays = models.CharField(max_length=20, blank=True, verbose_name="Hari libur",
        help_text="Hari tanpa pengiriman.")

    @property
    def closed_days(self):
        return {int(day) for day in self.closed_weekdays.split(",") if day.strip().isdigit()}


class DeliverySlot(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="delivery_slots")
    start_time = models.TimeField(verbose_name="Mulai")
    end_time = models.TimeField(verbose_name="Selesai")
    capacity = models.PositiveIntegerField(null=True, blank=True, verbose_name="Maks. pesanan",
        help_text="Per tanggal. Kosongkan untuk tanpa batas.")
    is_active = models.BooleanField(default=True, verbose_name="Aktif")

    class Meta:
        ordering = ["start_time", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(end_time__gt=models.F("start_time")),
            name="delivery_slot_ends_after_start")]

    @property
    def label(self):
        return f"{self.start_time:%H.%M}–{self.end_time:%H.%M}"

    def __str__(self):
        return self.label


class GiftCard(models.Model):
    """Store credit with a code and a balance, issued by the seller (giveaways, compensation,
    cards sold in person). Redeemed at checkout like a discount; restored if the order is
    cancelled. Every change is a GiftCardEntry."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="gift_cards")
    code = models.CharField(max_length=19)
    initial_value = models.DecimalField(max_digits=14, decimal_places=2)
    balance = models.DecimalField(max_digits=14, decimal_places=2)
    recipient_name = models.CharField(max_length=120, blank=True)
    recipient_email = models.EmailField(blank=True)
    note = models.CharField(max_length=200, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_store_gift_card"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="gift_card_balance_nonnegative")]

    def __str__(self): return f"{self.code} · Rp{self.balance:,.0f}"

    @property
    def masked(self): return f"•••• {self.code[-4:]}"


class GiftCardEntry(models.Model):
    KINDS = [("issue", "Diterbitkan"), ("redeem", "Dipakai"), ("restore", "Dikembalikan"),
        ("adjust", "Disesuaikan")]
    gift_card = models.ForeignKey(GiftCard, on_delete=models.CASCADE, related_name="entries")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="gift_card_entries")
    kind = models.CharField(max_length=10, choices=KINDS)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    actor = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
