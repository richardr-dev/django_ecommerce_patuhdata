from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models, transaction
from django.db.models import Case, Exists, F, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest

from apps.shops.models import Store
from apps.shops.uploads import StoreUploadPath

from .private_files import DigitalUploadPath, private_storage_callable


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
        raise ValidationError({"sku": f"SKU {sku} sudah dipakai produk atau varian lain di perusahaan ini."})


def _lock_store(store_id):
    Store.objects.select_for_update().filter(id=store_id).exists()


class Category(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    image = models.FileField(upload_to=StoreUploadPath("categories"), blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Ikon kategori di beranda, persegi (misalnya 300 × 300 px). Kosongkan untuk "
            "memakai foto produk pertama di kategori ini.")
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children",
        verbose_name="Induk kategori", help_text="Kosongkan untuk kategori utama. Paling dalam 3 tingkat.")

    MAX_DEPTH = 3
    SEPARATOR = " › "

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "slug"], name="unique_category_slug")]

    def __str__(self): return self.path

    @property
    def lineage(self):
        """From the main category down to this one: ``[Sepatu, Sneakers]``."""
        chain, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    @property
    def path(self):
        """"Sepatu › Sneakers"."""
        return self.SEPARATOR.join(node.name for node in self.lineage)

    @property
    def root(self):
        return self.lineage[0]

    @property
    def product_count(self):
        return self.product_set.count()

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.parent_id:
            if self.parent.store_id != self.store_id:
                raise ValidationError({"parent": "Induk kategori harus dari perusahaan yang sama."})
            node = self.parent
            while node is not None:
                if self.pk and node.pk == self.pk:
                    raise ValidationError({"parent": "Kategori tidak bisa menjadi induk dirinya sendiri."})
                node = node.parent
            if len(self.parent.lineage) + 1 > self.MAX_DEPTH:
                raise ValidationError({"parent": f"Paling dalam {self.MAX_DEPTH} tingkat kategori."})

class Product(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=160)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    compare_at_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Digital product: no shipping and no stock; the buyer downloads it after paying.
    is_digital = models.BooleanField("Produk digital", default=False,
        help_text="E-book, template, kelas online, dll. Tanpa pengiriman dan tanpa stok gudang.")
    digital_file = models.FileField("File digital", blank=True, max_length=300,
        storage=private_storage_callable, upload_to=DigitalUploadPath(),
        help_text="Maksimal 50 MB. Hanya pembeli yang sudah membayar bisa mengunduh.")
    digital_url = models.URLField("Link digital", blank=True, max_length=500,
        help_text="Atau link (Google Drive, kelas online). Dibuka pembeli setelah membayar.")
    cost_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Harga pokok (modal) per unit. Dipakai untuk menghitung HPP dan laba.")
    # The unit stock is counted in (pcs, botol, kg...). Larger units (dus = 12 pcs) are ``ProductUnit`` rows;
    # stock, cost and every movement stay in this unit.
    base_unit = models.CharField("Satuan dasar", max_length=20, default="pcs", blank=True)
    # Lot / batch and expiry tracked (food, cosmetics, medicine): receipts open lots, everything that leaves draws
    # the lot that expires first (``orders/lots.py``).
    tracks_lots = models.BooleanField("Lacak lot & kedaluwarsa", default=False)
    # Special wholesale price charged to the mitra (reseller/partner). Overrides regular price
    # on reseller sell-in orders. Leave blank to use the regular retail price.
    ab_shop_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
        verbose_name="Harga mitra",
        help_text="Harga khusus untuk mitra / reseller. Kosongkan jika sama dengan harga normal.")
    sku = models.CharField(max_length=80)
    image_url = models.URLField(blank=True)
    photo = models.FileField(upload_to=StoreUploadPath("products"), blank=True)
    weight_grams = models.PositiveIntegerField(default=0)
    length_cm = models.PositiveIntegerField(default=0)
    width_cm = models.PositiveIntegerField(default=0)
    height_cm = models.PositiveIntegerField(default=0)
    # Descriptive attributes (Merek, Barcode, Warna, Ukuran, Bahan) and any others the company keeps
    # ({"Garansi": "1 tahun"}). They describe the product; prices, stock and the books never read them.
    brand = models.CharField("Merek", max_length=80, blank=True)
    barcode = models.CharField("Barcode", max_length=60, blank=True)
    color = models.CharField("Warna", max_length=60, blank=True)
    size = models.CharField("Ukuran", max_length=40, blank=True)
    material = models.CharField("Bahan", max_length=80, blank=True)
    attributes = models.JSONField("Atribut lain", default=dict, blank=True)
    # The listing fields Shopee and Tokopedia / TikTok Shop ask for when a product is uploaded. Descriptive only:
    # prices, stock and the books never read them (category-specific extras go into ``attributes``).
    CONDITIONS = [("new", "Baru"), ("used", "Bekas")]
    WARRANTIES = [("", "Tidak ada garansi"), ("shop", "Garansi toko"), ("distributor", "Garansi distributor"),
        ("manufacturer", "Garansi resmi produsen"), ("international", "Garansi internasional")]
    DANGEROUS = [("", "Tidak berbahaya"), ("battery", "Mengandung baterai"), ("liquid", "Cairan"),
        ("flammable", "Mudah terbakar"), ("magnet", "Mengandung magnet")]
    condition = models.CharField("Kondisi", max_length=10, choices=CONDITIONS, default="new")
    model_number = models.CharField("Model / no. artikel", max_length=80, blank=True)
    min_order = models.PositiveSmallIntegerField("Minimal pembelian", default=1)
    max_order = models.PositiveSmallIntegerField("Maksimal pembelian", null=True, blank=True,
        help_text="Kosong: tanpa batas.")
    country_of_origin = models.CharField("Negara asal", max_length=60, blank=True)
    warranty_type = models.CharField("Jenis garansi", max_length=20, choices=WARRANTIES, blank=True)
    warranty_months = models.PositiveSmallIntegerField("Masa garansi (bulan)", null=True, blank=True)
    shelf_life_days = models.PositiveSmallIntegerField("Masa simpan (hari)", null=True, blank=True,
        help_text="Untuk makanan, minuman, kosmetik.")
    bpom_number = models.CharField("No. BPOM", max_length=40, blank=True)
    halal_number = models.CharField("No. sertifikat halal", max_length=60, blank=True)
    sni_number = models.CharField("No. SNI", max_length=40, blank=True)
    dangerous_goods = models.CharField("Barang berbahaya", max_length=20, choices=DANGEROUS, blank=True)
    insurance_required = models.BooleanField("Wajib asuransi pengiriman", default=False)
    showcase = models.CharField("Etalase toko", max_length=80, blank=True,
        help_text="Etalase di Tokopedia / kategori toko di Shopee.")
    video_url = models.URLField("Link video produk", blank=True, max_length=300)
    size_chart_url = models.URLField("Link tabel ukuran", blank=True, max_length=300)
    shopee_category = models.CharField("Kategori Shopee", max_length=160, blank=True,
        help_text="Jalur kategori, mis. Fashion Pria > Sepatu > Sneakers.")
    tokopedia_category = models.CharField("Kategori Tokopedia / TikTok Shop", max_length=160, blank=True)
    seo_title = models.CharField(max_length=160, blank=True)
    seo_description = models.CharField(max_length=320, blank=True)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False,
        help_text="Show this product in the featured section on the homepage.")
    # Made to order: sold against a daily quota instead of warehouse stock, delivered on a date
    # the shopper picks at checkout.
    is_preorder = models.BooleanField(default=False, verbose_name="Pre-order",
        help_text="Dibuat setelah dipesan. Pembeli memilih tanggal kirim; stok gudang tidak dipakai.")
    preorder_days = models.PositiveSmallIntegerField(default=1, verbose_name="Waktu produksi (hari)",
        help_text="Tanggal kirim paling cepat: hari ini + waktu produksi.")
    preorder_daily_quota = models.PositiveIntegerField(null=True, blank=True,
        verbose_name="Kuota per hari", help_text="Jumlah maksimal yang bisa dibuat untuk satu tanggal "
            "kirim. Kosongkan untuk tanpa batas.")
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

    @property
    def dimensions(self):
        """"30 × 20 × 12 cm", or "" when not measured."""
        if not (self.length_cm or self.width_cm or self.height_cm):
            return ""
        return f"{self.length_cm} × {self.width_cm} × {self.height_cm} cm"

    @property
    def volume_weight_grams(self):
        """Couriers' volume weight: P × L × T (cm) ÷ 6000 in kg, as grams. 0 when not measured."""
        return round(self.length_cm * self.width_cm * self.height_cm / 6)

    @property
    def shipping_weight_grams(self):
        """What a courier charges for: the heavier of the real weight and the volume weight."""
        return max(self.weight_grams or 0, self.volume_weight_grams)

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
        if self.is_digital:
            return DIGITAL_STOCK  # downloads never run out
        variants = [variant for variant in self.variants.all() if variant.is_active]
        if variants:
            return sum(variant.available_stock for variant in variants)
        return sum(level.available for level in self.inventory_levels.all())


# What a digital product reports as stock: never sold out, but a sane number for carts and lists.
DIGITAL_STOCK = 999


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
        When(is_digital=True, then=Value(DIGITAL_STOCK)),
        When(Exists(active_variants), then=Coalesce(Subquery(variants), Value(0))),
        default=Coalesce(Subquery(levels), Value(0)),
        output_field=models.IntegerField()))


class WholesalePrice(models.Model):
    """Harga grosir: from ``min_quantity`` units of this product in one order (all variants
    counted together), each unit costs ``price``. Variants with their own price drop by the
    same percentage. Not combined with a running flash sale."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="wholesale_prices")
    min_quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["min_quantity"]
        constraints = [
            models.UniqueConstraint(fields=["product", "min_quantity"], name="unique_wholesale_tier"),
            models.CheckConstraint(condition=models.Q(min_quantity__gte=2), name="wholesale_min_qty_gte_2"),
            models.CheckConstraint(condition=models.Q(price__gt=0), name="wholesale_price_positive"),
        ]

    def __str__(self):
        return f"{self.product.name}: ≥{self.min_quantity} @ {self.price}"


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
    postal_code = models.CharField(max_length=5, blank=True, verbose_name="Kode pos",
        validators=[RegexValidator(r"^\d{5}$", "Kode pos terdiri dari 5 angka.")],
        help_text="Ongkir otomatis dihitung dari lokasi ini (gudang aktif dengan prioritas terkecil).")
    # Chosen from the courier provider's own area list, so rates and pickups use exactly this place.
    area_id = models.CharField(max_length=80, blank=True)
    area_name = models.CharField(max_length=200, blank=True, verbose_name="Kecamatan / kota")
    # Map pin of the pickup point; same-day couriers (GoSend, Grab) need coordinates.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower values allocate first")
    is_active = models.BooleanField(default=True)
    # An outlet is a stock location that is also a shop customers can visit. Outlets are billed
    # per outlet on the plan; plain warehouses are free.
    is_outlet = models.BooleanField(default=False, verbose_name="Outlet / cabang",
        help_text="Toko fisik yang bisa dikunjungi pembeli. Outlet tambahan berbayar per outlet.")
    phone = models.CharField(max_length=30, blank=True, verbose_name="Telepon outlet")
    opening_hours = models.CharField(max_length=120, blank=True, verbose_name="Jam buka",
        help_text="Mis. Setiap hari 08.00–20.00")
    pickup_enabled = models.BooleanField(default=False, verbose_name="Bisa ambil di outlet",
        help_text="Pembeli bisa memilih ambil sendiri di outlet ini saat checkout.")
    # Where this location's sales and costs are tracked in the books (POS postings pass it), so the
    # cost-centre P&L shows each outlet while the company keeps one ledger.
    cost_center = models.ForeignKey("accounting.CostCenter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="warehouses", verbose_name="Pusat biaya")
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


class VariantStock(models.Model):
    """Where a variant's units are: one row per warehouse. ``ProductVariant.stock`` stays the store-wide total
    that every screen and the storefront read; these rows break it down by location and always add up to it.
    They are kept by ``orders/variant_stock.py`` from inside ``record_movement``, never written directly.
    Reservations stay store-wide on the variant."""
    variant = models.ForeignKey("ProductVariant", on_delete=models.CASCADE, related_name="locations")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="variant_stock")
    on_hand = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["warehouse__priority", "warehouse_id"]
        constraints = [models.UniqueConstraint(fields=["variant", "warehouse"], name="unique_variant_warehouse")]


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    url = models.URLField(blank=True)
    file = models.FileField(upload_to=StoreUploadPath("products"), blank=True)
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
    cost_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Kosongkan untuk memakai harga pokok produk induk.")
    # Special the mitra reseller price for this variant; falls back to product-level ab_shop_price.
    ab_shop_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
        verbose_name="Harga mitra",
        help_text="Harga khusus mitra untuk varian ini. Kosongkan untuk memakai harga produk induk.")
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
    def effective_ab_shop_price(self):
        """the mitra price for this variant; falls back to product-level price."""
        if self.ab_shop_price is not None:
            return self.ab_shop_price
        if self.product.ab_shop_price is not None:
            return self.product.ab_shop_price
        return self.effective_price

    @property
    def available_stock(self):
        return max(0, self.stock - self.reserved)

    def __str__(self):
        return f"{self.product.name} / {self.name}"


class ProductReview(models.Model):
    """A rating left by a customer who received the product, from their account or from the
    order page (guests). Merchants can hide a review or reply to it, but never author or edit
    one, so ratings stay genuine."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="product_reviews",
        null=True, blank=True, help_text="Empty for a guest's review, verified by its order.")
    order = models.ForeignKey("orders.Order", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="product_reviews", help_text="Delivered order that verifies the purchase.")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    title = models.CharField(max_length=120, blank=True)
    body = models.TextField(max_length=2000)
    display_name = models.CharField(max_length=60)
    variant_name = models.CharField(max_length=120, blank=True)
    is_published = models.BooleanField(default=True)
    merchant_reply = models.TextField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "user"], name="unique_product_review_per_user"),
            models.UniqueConstraint(fields=["product", "order"], condition=models.Q(
                order__isnull=False), name="unique_product_review_per_order"),
            models.CheckConstraint(condition=models.Q(rating__gte=1, rating__lte=5),
                name="product_review_rating_range"),
        ]
        indexes = [models.Index(fields=["product", "is_published", "-created_at"],
            name="review_product_published_idx")]

    def __str__(self):
        return f"{self.product.name} · {self.rating}★"


class ReviewPhoto(models.Model):
    """A buyer's photo of what arrived, shown with their review (at most 3)."""
    review = models.ForeignKey(ProductReview, on_delete=models.CASCADE, related_name="photos")
    file = models.FileField(upload_to=StoreUploadPath("reviews"),
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    @property
    def store(self):  # for StoreUploadPath: stores/<store>/reviews/…
        return self.review.store


class WishlistItem(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="wishlist_items")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="wishlist_items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="wishlist_items")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "product"], name="unique_wishlist_item")]


class Bundle(models.Model):
    """Paket: one sellable SKU made of several stock items, e.g. "Paket Kopi + Teh". Selling one
    takes each component off the shelf; the sale price is shared between the components."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="bundles")
    sku = models.CharField(max_length=80)
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["store", "sku"], name="unique_bundle_sku")]

    def __str__(self):
        return f"{self.sku} · {self.name}"

    @property
    def available_stock(self):
        """How many complete bundles the components allow."""
        counts = []
        for item in self.items.select_related("product", "variant"):
            stock = item.variant.available_stock if item.variant_id else item.product.available_stock
            counts.append(stock // item.quantity)
        return min(counts) if counts else 0


class BundleItem(models.Model):
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+")
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0),
            name="bundle_item_quantity_positive")]

    @property
    def sku(self):
        return self.variant.sku if self.variant_id else self.product.sku

    @property
    def unit_price(self):
        return self.variant.price if self.variant_id and getattr(self.variant, "price", None) \
            else self.product.price


class PriceList(models.Model):
    """A customer group's prices (grosir, reseller, proyek). A product with its own row sells at that
    price; any other product sells at retail less the list's discount. A customer carries one list
    (``Customer.price_list``) and the invoice form prices its lines from it (``tax/pricing.py``)."""
    store = models.ForeignKey("shops.Store", on_delete=models.CASCADE, related_name="price_lists")
    name = models.CharField(max_length=120)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0,
        help_text="Potongan dari harga jual untuk produk yang tidak punya harga khusus di daftar ini.")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["store", "name"], name="unique_store_price_list")]

    def __str__(self):
        return self.name


class PriceListItem(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="list_prices")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True,
        related_name="list_prices")
    price = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["product__name"]
        constraints = [models.UniqueConstraint(fields=["price_list", "product", "variant"],
            name="unique_price_list_product_variant")]


class ProductUnit(models.Model):
    """A larger unit a product is bought or sold in: "dus" holding ``factor`` base units (pcs). A line in this
    unit moves ``quantity × factor`` base units; its price is per this unit. See ``catalog/units.py``."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="units")
    name = models.CharField(max_length=20)
    factor = models.PositiveIntegerField()
    barcode = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["factor", "name"]
        constraints = [
            models.UniqueConstraint(fields=["product", "name"], name="unique_product_unit_name"),
            models.CheckConstraint(condition=models.Q(factor__gte=2), name="product_unit_factor_gt_one"),
        ]

    def __str__(self):
        return f"{self.name} ({self.factor} {self.product.base_unit or 'pcs'})"
