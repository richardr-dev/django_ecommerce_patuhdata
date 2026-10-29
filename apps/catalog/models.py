from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Case, Exists, F, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest

from apps.shops.models import Store


def ensure_store_sku_available(store_id, sku, *, product_id=None, variant_id=None):
    """SKUs identify order lines and stock, so they must be unique per store across both
    products and variants. Callers that write must hold the store row lock (see save())."""
    if not store_id or not sku:
        return
    products = Product.objects.filter(store_id=store_id, sku=sku)
    variants = ProductVariant.objects.filter(product__store_id=store_id, sku=sku)
    if product_id:
        products = products.exclude(id=product_id)
    if variant_id:
        variants = variants.exclude(id=variant_id)
    if products.exists() or variants.exists():
        raise ValidationError({"sku": f"SKU {sku} sudah dipakai produk atau varian lain di toko ini."})


def _lock_store(store_id):
    Store.objects.select_for_update().filter(id=store_id).exists()


class Category(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "slug"], name="unique_category_slug")]
    def __str__(self): return self.name

class Product(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=160)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    compare_at_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sku = models.CharField(max_length=80)
    image_url = models.URLField(blank=True)
    photo = models.FileField(upload_to="products/%Y/%m/", blank=True)
    weight_grams = models.PositiveIntegerField(default=0)
    length_cm = models.PositiveIntegerField(default=0)
    width_cm = models.PositiveIntegerField(default=0)
    height_cm = models.PositiveIntegerField(default=0)
    seo_title = models.CharField(max_length=160, blank=True)
    seo_description = models.CharField(max_length=320, blank=True)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False,
        help_text="Show this product in the featured section on the homepage.")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["store", "slug"], name="unique_product_slug"),
            models.UniqueConstraint(fields=["store", "sku"], name="unique_product_sku"),
        ]
        # Storefront browsing filters on store + is_active and sorts by one of these columns;
        # composite indexes keep paginated catalog pages fast at 10,000+ SKUs per store.
        indexes = [
            models.Index(fields=["store", "is_active", "-created_at", "-id"], name="product_newest_idx"),
            models.Index(fields=["store", "is_active", "price"], name="product_price_idx"),
            models.Index(fields=["store", "is_active", "name"], name="product_name_idx"),
            models.Index(fields=["store", "category", "is_active"], name="product_category_idx"),
            models.Index(fields=["store", "is_featured", "is_active"], name="product_featured_idx"),
        ]
    def __str__(self): return self.name

    def clean(self):
        super().clean()
        ensure_store_sku_available(self.store_id, self.sku, product_id=self.pk)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "sku" not in update_fields:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            _lock_store(self.store_id)
            ensure_store_sku_available(self.store_id, self.sku, product_id=self.pk)
            return super().save(*args, **kwargs)

    @property
    def has_discount(self):
        return bool(self.compare_at_price and self.compare_at_price > self.price)

    @property
    def discount_percent(self):
        if not self.has_discount:
            return 0
        return round((self.compare_at_price - self.price) / self.compare_at_price * 100)

    @property
    def primary_image_url(self):
        return self.photo.url if self.photo else self.image_url

    @property
    def available_stock(self):
        variants = [variant for variant in self.variants.all() if variant.is_active]
        if variants:
            return sum(variant.available_stock for variant in variants)
        return sum(level.available for level in self.inventory_levels.all())


def annotate_available_stock(queryset):
    """Annotate `stock_available` in SQL, matching Product.available_stock, so stock filters
    and sorts over large catalogs never load every product and inventory row into Python."""
    levels = (InventoryLevel.objects.filter(product=OuterRef("pk")).values("product")
        .annotate(total=Sum(Greatest(F("on_hand") - F("reserved") - F("safety_stock"), Value(0))))
        .values("total"))
    active_variants = ProductVariant.objects.filter(product=OuterRef("pk"), is_active=True)
    variants = (active_variants.values("product")
        .annotate(total=Sum(Greatest(F("stock") - F("reserved"), Value(0)))).values("total"))
    return queryset.annotate(stock_available=Case(
        When(Exists(active_variants), then=Coalesce(Subquery(variants), Value(0))),
        default=Coalesce(Subquery(levels), Value(0)),
        output_field=models.IntegerField()))


class FlashSaleProduct(models.Model):
    campaign = models.ForeignKey("shops.MarketingCampaign", on_delete=models.CASCADE, related_name="product_offers")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="flash_sale_offers")
    sale_price = models.DecimalField(max_digits=14, decimal_places=2)
    stock_limit = models.PositiveIntegerField(null=True, blank=True,
        help_text="Optional maximum units sold during this campaign.")
    per_customer_limit = models.PositiveIntegerField(null=True, blank=True,
        help_text="Optional maximum units per customer phone number.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["campaign", "product"], name="unique_campaign_product_offer"),
            models.CheckConstraint(condition=models.Q(sale_price__gt=0), name="flash_sale_price_positive"),
        ]

    def __str__(self):
        return f"{self.campaign.name} / {self.product.name}"


class Warehouse(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="warehouses")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=40)
    address = models.TextField(blank=True)
    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower values allocate first")
    is_active = models.BooleanField(default=True)
    class Meta:
        ordering = ["priority", "id"]
        constraints = [models.UniqueConstraint(fields=["store", "code"], name="unique_warehouse_code")]
    def __str__(self): return f"{self.store.name} / {self.name}"

class InventoryLevel(models.Model):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="inventory_levels")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="inventory_levels")
    on_hand = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    safety_stock = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "product"], name="unique_warehouse_product"),
            models.CheckConstraint(condition=models.Q(on_hand__gte=models.F("reserved")), name="inventory_reserved_lte_on_hand"),
        ]
    @property
    def available(self): return max(0, self.on_hand - self.reserved - self.safety_stock)

    def clean(self):
        if self.product_id and self.warehouse_id and self.product.store_id != self.warehouse.store_id:
            raise ValidationError("Product and warehouse must belong to the same store.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    url = models.URLField(blank=True)
    file = models.FileField(upload_to="products/gallery/%Y/%m/", blank=True)
    alt_text = models.CharField(max_length=180, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    @property
    def image_url(self):
        return self.file.url if self.file else self.url


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=160)
    sku = models.CharField(max_length=80)
    attributes = models.JSONField(default=dict, help_text='Example: {"size": "M", "color": "Black"}')
    price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    weight_grams = models.PositiveIntegerField(null=True, blank=True)
    stock = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    barcode = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "sku"], name="unique_product_variant_sku"),
            models.CheckConstraint(condition=models.Q(stock__gte=models.F("reserved")),
                name="variant_reserved_lte_stock"),
        ]
        indexes = [models.Index(fields=["sku"], name="variant_sku_idx")]

    def clean(self):
        super().clean()
        if self.product_id:
            ensure_store_sku_available(self.product.store_id, self.sku, variant_id=self.pk)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "sku" not in update_fields:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            store_id = Product.objects.values_list("store_id", flat=True).get(id=self.product_id)
            _lock_store(store_id)
            ensure_store_sku_available(store_id, self.sku, variant_id=self.pk)
            return super().save(*args, **kwargs)

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.price

    @property
    def available_stock(self):
        return max(0, self.stock - self.reserved)

    def __str__(self):
        return f"{self.product.name} / {self.name}"


class ProductReview(models.Model):
    """A rating left by a signed-in customer who received the product. Merchants can hide a
    review or reply to it, but never author or edit one, so ratings stay genuine."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="product_reviews")
    order = models.ForeignKey("orders.Order", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="product_reviews", help_text="Delivered order that verifies the purchase.")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    title = models.CharField(max_length=120, blank=True)
    body = models.TextField(max_length=2000)
    display_name = models.CharField(max_length=60)
    is_published = models.BooleanField(default=True)
    merchant_reply = models.TextField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "user"], name="unique_product_review_per_user"),
            models.CheckConstraint(condition=models.Q(rating__gte=1, rating__lte=5),
                name="product_review_rating_range"),
        ]
        indexes = [models.Index(fields=["product", "is_published", "-created_at"],
            name="review_product_published_idx")]

    def __str__(self):
        return f"{self.product.name} · {self.rating}★"


class WishlistItem(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="wishlist_items")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="wishlist_items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="wishlist_items")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "product"], name="unique_wishlist_item")]
