from django.core.exceptions import ValidationError
from django.db import models

from apps.shops.models import Store


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
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["store", "slug"], name="unique_product_slug"),
            models.UniqueConstraint(fields=["store", "sku"], name="unique_product_sku"),
        ]
    def __str__(self): return self.name

    @property
    def primary_image_url(self):
        return self.photo.url if self.photo else self.image_url

    @property
    def available_stock(self):
        variants = [variant for variant in self.variants.all() if variant.is_active]
        if variants:
            return sum(variant.available_stock for variant in variants)
        return sum(level.available for level in self.inventory_levels.all())

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

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.price

    @property
    def available_stock(self):
        return max(0, self.stock - self.reserved)

    def __str__(self):
        return f"{self.product.name} / {self.name}"
