import re
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.db import models

from apps.billing.plans import outlet_limit_error, product_limit_error
from apps.catalog.models import (
    Category,
    FlashSaleProduct,
    InventoryLevel,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Warehouse,
    WholesalePrice,
)
from apps.catalog.reviews import MAX_REVIEW_PHOTOS, is_image
from apps.shops.forms import clean_instagram_handle
from apps.shops.models import MarketingCampaign, Store, StorefrontSlide, StorePage
from apps.shops.tenancy import custom_domain_allowed

from .models import Coupon, CustomerAddress, ShippingMethod
from .wholesale import MAX_TIERS

DOMAIN_PATTERN = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class MultipleImageInput(forms.FileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    """Several photos from one file picker, each checked like a single upload."""

    def __init__(self, *, max_files=10, **kwargs):
        self.max_files = max_files
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", MultipleImageInput(attrs={
            "accept": "image/png,image/jpeg,image/webp"}))
        super().__init__(**kwargs)

    def clean(self, data, initial=None):
        files = [upload for upload in (data if isinstance(data, list | tuple) else [data]) if upload]
        if len(files) > self.max_files:
            raise forms.ValidationError(f"Maksimal {self.max_files} foto sekaligus.")
        for upload in files:
            if getattr(upload, "content_type", "") not in IMAGE_CONTENT_TYPES:
                raise forms.ValidationError(f"{upload.name}: format harus PNG, JPG, atau WebP.")
            if upload.size > MAX_IMAGE_BYTES:
                raise forms.ValidationError(f"{upload.name}: ukuran maksimal 5 MB.")
        return files


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")


class ProductSkuField(forms.CharField):
    """Pick a product by typing its SKU. A <select> listing every product becomes unusable,
    and multi-megabyte, once a store carries thousands of SKUs."""

    def __init__(self, **kwargs):
        kwargs.setdefault("label", "SKU produk")
        kwargs.setdefault("help_text", "Ketik SKU produk induk, misalnya SKU-001.")
        super().__init__(max_length=80, **kwargs)
        self.store = None

    def prepare_value(self, value):
        if isinstance(value, Product):
            return value.sku
        if isinstance(value, int):
            return Product.objects.filter(id=value).values_list("sku", flat=True).first() or ""
        return value

    def clean(self, value):
        products = Product.objects.filter(store=self.store)
        if isinstance(value, Product):
            value = value.id
        if isinstance(value, int):
            # Disabled fields are cleaned from their initial value, which is the product id.
            product = products.filter(id=value).first()
        else:
            sku = super().clean(value).strip()
            product = products.filter(sku=sku).first() if sku else None
        if product is None:
            raise forms.ValidationError("Produk dengan SKU ini tidak ditemukan di toko ini.")
        return product


class ProductSkuFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].store = self.store


class ProductForm(StyledModelForm):
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), required=False,
        label="Gudang awal", help_text="Lokasi stok awal produk ini.")
    initial_stock = forms.IntegerField(min_value=0, initial=0, required=False,
        label="Stok awal", help_text="Dapat diubah kemudian melalui menu Kelola stok.")
    photos = MultipleImageField(label="Tambah foto produk",
        help_text="Pilih beberapa foto sekaligus (maksimal 10, masing-masing 5 MB). Foto pertama "
            "menjadi foto utama bila produk belum punya foto.")

    class Meta:
        model = Product
        exclude = ("store", "created_at")
        widgets = {"description": forms.Textarea(attrs={"rows": 4}), "seo_description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.store and not self.instance.store_id:
            self.instance.store = self.store
        self.fields["category"].queryset = Category.objects.filter(store=self.store)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(store=self.store, is_active=True)
        self.stock_level = None
        if self.instance.pk:
            self.fields.pop("warehouse")
            self.fields.pop("initial_stock")
            self._add_stock_field()
        self.fields["preorder_days"].required = False
        self._add_wholesale_fields()

    def _add_wholesale_fields(self):
        """Harga grosir rows, shown in their own card (see merchant/product_wholesale.html)."""
        saved = list(self.instance.wholesale_prices.all()) if self.instance.pk else []
        for index in range(1, MAX_TIERS + 1):
            tier = saved[index - 1] if index <= len(saved) else None
            quantity = forms.IntegerField(min_value=2, required=False, label="Minimal beli",
                initial=tier.min_quantity if tier else None)
            price = forms.DecimalField(min_value=Decimal(1), max_digits=14, decimal_places=2,
                required=False, label="Harga per pcs", initial=tier.price if tier else None)
            quantity.wholesale = price.wholesale = True
            quantity.widget.attrs.update({"class": "control", "placeholder": "mis. 10"})
            price.widget.attrs.update({"class": "control", "placeholder": "mis. 45000"})
            self.fields[f"wholesale_min_{index}"] = quantity
            self.fields[f"wholesale_price_{index}"] = price

    @property
    def wholesale_rows(self):
        return [(self[f"wholesale_min_{index}"], self[f"wholesale_price_{index}"])
            for index in range(1, MAX_TIERS + 1)]

    def _clean_wholesale(self, cleaned):
        tiers = []
        for index in range(1, MAX_TIERS + 1):
            quantity = cleaned.get(f"wholesale_min_{index}")
            price = cleaned.get(f"wholesale_price_{index}")
            if quantity is None and price is None:
                continue
            if quantity is None or price is None:
                self.add_error(f"wholesale_min_{index}" if quantity is None else
                    f"wholesale_price_{index}", "Isi jumlah minimal dan harganya.")
                continue
            tiers.append((quantity, price, index))
        tiers.sort()
        regular = cleaned.get("price")
        previous_price = regular
        seen = set()
        for quantity, price, index in tiers:
            if quantity in seen:
                self.add_error(f"wholesale_min_{index}", f"Jumlah {quantity} dipakai dua kali.")
            elif previous_price is not None and price >= previous_price:
                self.add_error(f"wholesale_price_{index}", "Harga grosir harus lebih murah dari "
                    "harga normal dan dari tingkat dengan jumlah lebih sedikit.")
            seen.add(quantity)
            previous_price = price
        self.wholesale_tiers = [(quantity, price) for quantity, price, _ in tiers]

    def save_wholesale(self, product):
        if not hasattr(self, "wholesale_tiers"):
            return
        current = {(tier.min_quantity, tier.price) for tier in product.wholesale_prices.all()}
        if current == set(self.wholesale_tiers):
            return
        product.wholesale_prices.all().delete()
        WholesalePrice.objects.bulk_create(WholesalePrice(product=product, min_quantity=quantity,
            price=price) for quantity, price in self.wholesale_tiers)

    def _add_stock_field(self):
        """Stock of a simple product in its main warehouse, editable right here. Products with
        variants keep stock per variant, and pre-orders have none (both in Kelola stok)."""
        product = self.instance
        if product.is_preorder or product.variants.filter(is_active=True).exists():
            return
        levels = product.inventory_levels.select_related("warehouse").filter(
            warehouse__is_active=True).order_by("warehouse__priority", "warehouse_id")
        self.stock_level = levels.first()
        warehouse = (self.stock_level.warehouse if self.stock_level else
            Warehouse.objects.filter(store=self.store, is_active=True).order_by("priority", "id").first())
        if warehouse is None:
            return
        self.stock_warehouse = warehouse
        reserved = self.stock_level.reserved if self.stock_level else 0
        others = levels.count() > 1
        self.fields["stock"] = forms.IntegerField(min_value=reserved, required=False,
            label=f"Stok di {warehouse.name}",
            initial=self.stock_level.on_hand if self.stock_level else 0,
            help_text=(f"{reserved} unit sedang dipesan dan tidak bisa dikurangi. " if reserved else "")
                + ("Stok gudang lain diatur di menu Kelola stok." if others else
                "Jumlah fisik barang yang siap dijual."))

    def save_stock(self, product, actor):
        """Apply the stock field, recording the change like Kelola stok does."""
        if "stock" not in self.fields or self.cleaned_data.get("stock") is None:
            return
        from django.db import transaction

        from .inventory import record_movement

        new_stock = self.cleaned_data["stock"]
        with transaction.atomic():
            level, _ = InventoryLevel.objects.select_for_update().get_or_create(
                warehouse=self.stock_warehouse, product=product)
            previous = level.on_hand
            if previous == new_stock or new_stock < level.reserved:
                return
            level.on_hand = new_stock
            level.save(update_fields=["on_hand", "updated_at"])
            record_movement(store=product.store, inventory_level=level, kind="adjust",
                quantity=abs(new_stock - previous), reason="Ubah stok di halaman produk",
                actor=actor, previous_on_hand=previous, new_on_hand=new_stock,
                previous_reserved=level.reserved, new_reserved=level.reserved)

    def clean_digital_file(self):
        upload = self.cleaned_data.get("digital_file")
        if upload and hasattr(upload, "size") and upload.size > 50 * 1024 * 1024:
            raise forms.ValidationError("Ukuran file digital maksimal 50 MB.")
        return upload

    def clean_preorder_days(self):
        days = self.cleaned_data.get("preorder_days")
        return 1 if days is None else days

    def clean(self):
        cleaned = super().clean()
        # self.instance still holds the saved values here; the form copies onto it afterwards.
        if cleaned.get("is_digital") and not (cleaned.get("digital_file") or cleaned.get("digital_url")):
            self.add_error("digital_file", "Unggah file atau isi link digital untuk produk digital.")
        self._clean_wholesale(cleaned)
        if self.store and cleaned.get("is_active") and not (self.instance.pk and self.instance.is_active):
            error = product_limit_error(self.store,
                Product.objects.filter(store=self.store, is_active=True).count() + 1)
            if error:
                raise forms.ValidationError(error)
        return cleaned

    def save_uploads(self, product):
        photos = self.cleaned_data.get("photos") or []
        if photos and not product.photo and not product.image_url:
            product.photo = photos.pop(0)
            product.save(update_fields=["photo"])
        position = (product.images.aggregate(top=models.Max("position"))["top"] or 0) + 1
        for offset, photo in enumerate(photos):
            ProductImage.objects.create(product=product, file=photo, alt_text=product.name[:180],
                position=position + offset)


class FlashSaleProductForm(ProductSkuFormMixin, StyledModelForm):
    product = ProductSkuField()

    class Meta:
        model = FlashSaleProduct
        fields = ("campaign", "product", "sale_price", "stock_limit", "per_customer_limit")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["campaign"].queryset = MarketingCampaign.objects.filter(store=self.store)

    def clean(self):
        cleaned = super().clean()
        product, sale_price = cleaned.get("product"), cleaned.get("sale_price")
        if product and sale_price:
            regular_prices = [product.price, *(variant.effective_price for variant in
                product.variants.filter(is_active=True))]
            if sale_price >= min(regular_prices):
                self.add_error("sale_price",
                    "Harga flash sale harus lebih rendah dari harga normal produk dan semua varian aktif.")
        return cleaned


class ProductImageForm(ProductSkuFormMixin, StyledModelForm):
    product = ProductSkuField()

    class Meta:
        model = ProductImage
        fields = ("product", "file", "url", "alt_text", "position")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("file") and not cleaned.get("url"):
            raise forms.ValidationError("Unggah file foto atau masukkan URL gambar.")
        return cleaned


class MerchantLoginForm(AuthenticationForm):
    error_messages = {**AuthenticationForm.error_messages,
        "invalid_login": "Email/nomor WhatsApp atau kata sandi salah. Periksa lagi, lalu coba "
            "masuk kembali."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Email atau nomor WhatsApp"
        self.fields["username"].widget.attrs.update({"autocomplete": "username",
            "placeholder": "nama@email.com atau 0812…", "autocapitalize": "none"})
        self.fields["password"].label = "Kata sandi"


class CustomerLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Email"
        self.fields["username"].widget.attrs.update({"autocomplete": "email"})


class CustomerRegistrationForm(UserCreationForm):
    email = forms.EmailField(label="Email")
    first_name = forms.CharField(label="Nama lengkap", max_length=150)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("email", "first_name", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("Email ini sudah terdaftar.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.username = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class CategoryForm(StyledModelForm):
    class Meta: model = Category; exclude = ("store",)


class WarehouseForm(StyledModelForm):
    """Gudang & outlet. Its place is picked from the courier provider's area list and a map pin
    (templates/merchant/warehouse_location.html), which fill the hidden location fields."""

    class Meta:
        model = Warehouse
        fields = ("name", "code", "area_id", "area_name", "postal_code", "latitude", "longitude",
            "address", "phone", "priority", "is_active", "is_outlet", "opening_hours",
            "pickup_enabled")
        labels = {"name": "Nama gudang / outlet", "code": "Kode", "address": "Alamat lengkap",
            "phone": "Telepon (dihubungi kurir)", "priority": "Prioritas stok",
            "is_active": "Aktif"}
        help_texts = {"address": "Jalan, nomor, gedung/unit, patokan. Kurir menjemput ke sini.",
            "priority": "Angka kecil dipakai lebih dulu untuk stok dan ongkir.",
            "code": "Singkatan unik, mis. JKT atau BDG."}
        widgets = {"area_id": forms.HiddenInput, "area_name": forms.HiddenInput,
            "latitude": forms.HiddenInput, "longitude": forms.HiddenInput,
            "address": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        was_counted = bool(self.instance.pk and self.instance.is_outlet and self.instance.is_active)
        if self.store and cleaned.get("is_outlet") and cleaned.get("is_active") and not was_counted:
            active = Warehouse.objects.filter(store=self.store, is_outlet=True, is_active=True)
            error = outlet_limit_error(self.store, active.exclude(pk=self.instance.pk).count() + 1)
            if error:
                raise forms.ValidationError(error)
        if cleaned.get("pickup_enabled") and not cleaned.get("is_outlet"):
            self.add_error("pickup_enabled", "Ambil di tempat hanya untuk outlet.")
        return cleaned


class InventoryForm(ProductSkuFormMixin, StyledModelForm):
    product = ProductSkuField()

    class Meta: model = InventoryLevel; fields = ("warehouse", "product", "on_hand", "safety_stock")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(store=self.store)
        self.fields["on_hand"].help_text = "Jumlah fisik di gudang. Stok yang sedang dipesan tetap terlindungi."
        self.fields["safety_stock"].help_text = "Jumlah minimum yang tidak ditawarkan ke pelanggan."
        if self.instance.pk:
            self.fields["warehouse"].disabled = True
            self.fields["product"].disabled = True


class ProductVariantRowForm(StyledModelForm):
    """One variant row inside the product page: its own name, SKU, price and stock."""

    class Meta:
        model = ProductVariant
        fields = ("name", "sku", "attributes", "price", "cost_price", "stock", "barcode",
            "weight_grams", "is_active")
        labels = {"name": "Varian", "sku": "SKU", "price": "Harga", "cost_price": "Harga pokok",
            "stock": "Stok", "barcode": "Barcode", "weight_grams": "Berat (gram)",
            "is_active": "Aktif"}
        widgets = {"attributes": forms.HiddenInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attributes"].required = False
        self.fields["price"].widget.attrs["placeholder"] = "Harga produk"
        self.fields["cost_price"].widget.attrs["placeholder"] = "Harga pokok produk"
        self.fields["weight_grams"].widget.attrs["placeholder"] = "Berat produk"
        self.fields["is_active"].initial = True
        reserved = self.instance.reserved if self.instance.pk else 0
        self.fields["stock"].min_value = reserved
        self.fields["stock"].widget.attrs["min"] = reserved
        if reserved:
            self.fields["stock"].help_text = f"{reserved} dipesan"

    def clean_attributes(self):
        value = self.cleaned_data.get("attributes")
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise forms.ValidationError("Atribut varian tidak valid.")
        return {str(key)[:40]: str(item)[:80] for key, item in value.items()}

    def clean_sku(self):
        return (self.cleaned_data.get("sku") or "").strip()

    def clean_stock(self):
        stock = self.cleaned_data.get("stock")
        if stock is not None and self.instance.pk and stock < self.instance.reserved:
            raise forms.ValidationError(
                f"Tidak boleh kurang dari {self.instance.reserved} unit yang sedang dipesan.")
        return stock


class BaseProductVariantFormSet(forms.BaseInlineFormSet):
    """The variants of a product, edited together with the product itself. Stock changes are
    logged like Kelola stok, and variants with order history are deactivated, not deleted."""

    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        self.product_sku = None
        super().__init__(*args, **kwargs)
        self.queryset = self.queryset.order_by("id")

    def get_form_kwargs(self, index):
        return {**super().get_form_kwargs(index), "store": self.store}

    def validate_unique(self):
        """Duplicate SKUs get a clearer message, per row, in clean()."""

    def live_forms(self):
        return [form for form in self.forms if getattr(form, "cleaned_data", None)
            and (form.has_changed() or form.instance.pk) and not self._should_delete_form(form)]

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = {}
        product = self.instance
        for form in self.live_forms():
            sku = form.cleaned_data.get("sku")
            if not sku:
                continue
            if self.product_sku and sku == self.product_sku:
                form.add_error("sku", "SKU varian tidak boleh sama dengan SKU produk.")
            elif sku in seen:
                form.add_error("sku", "SKU ini dipakai dua kali di produk ini.")
            seen[sku] = form
        if not seen or not self.store:
            return
        own_variants = ProductVariant.objects.filter(product=product) if product.pk else (
            ProductVariant.objects.none())
        taken = set(Product.objects.filter(store=self.store, sku__in=seen).exclude(
            pk=product.pk).values_list("sku", flat=True))
        taken |= set(ProductVariant.objects.filter(product__store=self.store, sku__in=seen)
            .exclude(pk__in=own_variants.values("pk")).values_list("sku", flat=True))
        for sku in taken:
            seen[sku].add_error("sku", f"SKU {sku} sudah dipakai produk atau varian lain di toko ini.")

    def save_all(self, product, actor):
        """Save every row. Returns how many variants were deactivated instead of deleted."""
        from django.db import transaction
        from django.db.models.deletion import ProtectedError

        from .inventory import record_movement

        self.instance = product
        kept = 0
        with transaction.atomic():
            for form in self.forms:
                variant = form.instance
                if self._should_delete_form(form):
                    if variant.pk:
                        try:
                            with transaction.atomic():
                                variant.delete()
                        except ProtectedError:
                            ProductVariant.objects.filter(pk=variant.pk).update(is_active=False)
                            kept += 1
                    continue
                if not form.has_changed() or not form.cleaned_data:
                    continue
                data = form.cleaned_data
                if variant.pk:
                    locked = ProductVariant.objects.select_for_update().get(pk=variant.pk)
                    previous = locked.stock
                    for name in ("name", "sku", "attributes", "price", "cost_price", "barcode",
                            "weight_grams", "is_active"):
                        setattr(locked, name, data.get(name))
                    new_stock = max(data["stock"], locked.reserved)
                    locked.stock = new_stock
                    locked.save()
                else:
                    locked = form.save(commit=False)
                    locked.product = product
                    previous, new_stock = 0, locked.stock
                    locked.save()
                if new_stock != previous:
                    record_movement(store=product.store, variant=locked, kind="adjust",
                        quantity=abs(new_stock - previous), reason="Ubah stok varian di halaman produk",
                        actor=actor, previous_on_hand=previous, new_on_hand=new_stock,
                        previous_reserved=locked.reserved, new_reserved=locked.reserved)
        return kept


ProductVariantFormSet = forms.inlineformset_factory(Product, ProductVariant,
    form=ProductVariantRowForm, formset=BaseProductVariantFormSet, extra=0, can_delete=True)


class VariantForm(ProductSkuFormMixin, StyledModelForm):
    product = ProductSkuField()

    class Meta: model = ProductVariant; fields = ("product", "name", "sku", "barcode", "attributes", "price", "cost_price", "weight_grams", "stock", "is_active")


class MarketingCampaignForm(StyledModelForm):
    class Meta:
        model = MarketingCampaign
        exclude = ("store",)
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean(self):
        cleaned = super().clean()
        starts_at, ends_at = cleaned.get("starts_at"), cleaned.get("ends_at")
        if starts_at and ends_at and ends_at <= starts_at:
            self.add_error("ends_at", "Waktu selesai harus setelah waktu mulai.")
        link = cleaned.get("link_url", "").strip()
        if link and not link.startswith(("/", "#", "https://", "http://")):
            self.add_error("link_url", "Gunakan path /..., anchor #..., atau URL http(s) lengkap.")
        image = cleaned.get("popup_image")
        if image and hasattr(image, "size") and image.size > 5 * 1024 * 1024:
            self.add_error("popup_image", "Ukuran gambar maksimal 5 MB.")
        return cleaned


class StorefrontSlideForm(StyledModelForm):
    class Meta:
        model = StorefrontSlide
        exclude = ("store",)

    def clean(self):
        cleaned = super().clean()
        for name in ("image", "mobile_image"):
            image = cleaned.get(name)
            if image and hasattr(image, "size") and image.size > 5 * 1024 * 1024:
                self.add_error(name, "Ukuran gambar maksimal 5 MB.")
        link = cleaned.get("link_url", "").strip()
        if link and not (link.startswith(("/", "https://", "http://"))):
            self.add_error("link_url", "Gunakan path /... atau URL http(s) lengkap.")
        return cleaned


class ShippingMethodForm(StyledModelForm):
    class Meta: model = ShippingMethod; exclude = ("store",)


class CouponForm(StyledModelForm):
    class Meta: model = Coupon; exclude = ("store", "used_count")


class StoreSettingsForm(StyledModelForm):
    class Meta:
        model = Store
        fields = ("name", "logo", "logo_size", "show_store_name", "domain", "description", "theme",
            "primary_color", "background_color", "font_family", "catalog_title",
            "announcement_text", "announcement_link_label", "announcement_link_url",
            "currency", "tax_rate",
            "support_phone", "instagram", "support_email", "terms_url", "privacy_url", "return_policy",
            "listed_in_directory")
        fields = fields + ("loyalty_enabled", "loyalty_points_per_step", "loyalty_spend_step",
            "loyalty_point_value", "loyalty_min_redeem")
        widgets = {"description": forms.Textarea(attrs={"rows": 3}),
            "return_policy": forms.Textarea(attrs={"rows": 4}),
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "background_color": forms.TextInput(attrs={"type": "color"})}

    banners = MultipleImageField(label="Tambah banner",
        help_text="Foto landscape, disarankan 1920 × 640 px (rasio 3:1), maksimal 5 MB. Pilih "
            "beberapa sekaligus; semuanya tampil bergantian di bagian atas toko.")
    remove_banners = forms.ModelMultipleChoiceField(queryset=StorefrontSlide.objects.none(),
        required=False, widget=forms.CheckboxSelectMultiple, label="Hapus banner")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            for name in ("loyalty_points_per_step", "loyalty_spend_step", "loyalty_point_value",
                    "loyalty_min_redeem", "logo_size", "background_color", "font_family"):
                if name not in self.data:
                    self.fields.pop(name)
        if "remove_banners" in self.fields:
            self.fields["remove_banners"].queryset = StorefrontSlide.objects.filter(
                store=self.instance)
        labels = {"catalog_title": "Judul katalog", "announcement_text": "Teks pengumuman",
            "announcement_link_label": "Teks tautan pengumuman",
            "announcement_link_url": "Tujuan pengumuman"}
        for name, label in labels.items(): self.fields[name].label = label
        if "domain" in self.fields:
            domain_field = self.fields["domain"]
            domain_field.label = "Domain sendiri"
            if custom_domain_allowed(self.instance):
                target = settings.PLATFORM_DOMAIN or "alamat IP server"
                domain_field.help_text = (f"Contoh: tokoanda.com. Arahkan DNS domain ke {target} "
                    "(CNAME untuk subdomain seperti www, A record untuk domain utama); "
                    "HTTPS aktif otomatis setelah DNS tersambung.")
            else:
                domain_field.disabled = True
                domain_field.help_text = "Domain sendiri tersedia di paket berbayar (menu Langganan)."

    def clean_instagram(self):
        return clean_instagram_handle(self.cleaned_data.get("instagram"))

    def clean_domain(self):
        domain = (self.cleaned_data.get("domain") or "").strip().lower().rstrip(".")
        if not domain:
            return None
        domain = domain.removeprefix("https://").removeprefix("http://").split("/")[0]
        platform = settings.PLATFORM_DOMAIN
        if platform and (domain == platform or domain.endswith(f".{platform}")):
            raise forms.ValidationError(f"Alamat {platform} sudah otomatis tersedia; isi domain milik Anda sendiri.")
        if not DOMAIN_PATTERN.fullmatch(domain):
            raise forms.ValidationError("Masukkan nama domain yang valid, misalnya tokoanda.com.")
        return domain
        self.fields["domain"].help_text = "Kosongkan saat lokal. Isi tanpa https://, misalnya tokoanda.id."
        self.fields["primary_color"].help_text = "Warna tombol, tautan, dan aksen toko."
        extra_labels = {"logo_size": "Ukuran logo", "show_store_name": "Tampilkan nama di samping logo",
            "theme": "Gaya tampilan", "background_color": "Warna latar halaman",
            "font_family": "Jenis huruf"}
        for name, label in extra_labels.items():
            if name in self.fields:
                self.fields[name].label = label

    def clean(self):
        cleaned = super().clean()
        for name in ("announcement_link_url",):
            link = cleaned.get(name, "").strip()
            if link and not link.startswith(("/", "https://", "http://", "#")):
                self.add_error(name, "Gunakan path /..., anchor #..., atau URL http(s) lengkap.")
        for name in ("loyalty_points_per_step", "loyalty_spend_step", "loyalty_point_value"):
            if cleaned.get(name) is not None and cleaned[name] <= 0:
                self.add_error(name, "Nilai harus lebih besar dari nol.")
        return cleaned

    def _clean_image(self, name, label):
        image = self.cleaned_data.get(name)
        if not image or not hasattr(image, "content_type"):
            return image
        if image.size > 5 * 1024 * 1024:
            raise forms.ValidationError(f"Ukuran {label} maksimal 5 MB.")
        if image.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise forms.ValidationError(f"Format {label} harus PNG, JPG, atau WebP.")
        return image

    def clean_logo(self):
        return self._clean_image("logo", "logo")

    def save_uploads(self, store):
        """Add the uploaded banners after the existing ones and delete the ticked ones."""
        removed = self.cleaned_data.get("remove_banners")
        if removed:
            removed.delete()
        position = (store.storefront_slides.aggregate(top=models.Max("position"))["top"] or 0) + 1
        for offset, image in enumerate(self.cleaned_data.get("banners") or []):
            StorefrontSlide.objects.create(store=store, image=image, position=position + offset)


class ProductReviewForm(StyledModelForm):
    rating = forms.TypedChoiceField(choices=[(n, f"{n} bintang") for n in range(5, 0, -1)],
        coerce=int, label="Penilaian")

    class Meta:
        model = ProductReview
        fields = ("rating", "title", "body")
        labels = {"title": "Judul ulasan", "body": "Ulasan Anda"}
        widgets = {"body": forms.Textarea(attrs={"rows": 4, "maxlength": 2000})}


class OrderReviewForm(forms.Form):
    """Penilaian from the order page: stars, an optional title, the review and up to 3 photos."""
    rating = forms.TypedChoiceField(choices=[(n, f"{n} bintang") for n in range(5, 0, -1)],
        coerce=int, widget=forms.RadioSelect, label="Penilaian",
        error_messages={"required": "Pilih jumlah bintang."})
    title = forms.CharField(max_length=120, required=False, label="Judul (opsional)")
    body = forms.CharField(max_length=2000, min_length=10, label="Ulasan",
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 2000,
            "placeholder": "Bagaimana kualitas produk, kemasan, dan kesesuaiannya dengan foto?"}),
        error_messages={"min_length": "Tulis minimal 10 karakter.",
            "required": "Tulis ulasan singkat tentang produk ini."})
    photos = MultipleImageField(max_files=MAX_REVIEW_PHOTOS, label="Foto (maks. 3)")

    def clean_photos(self):
        photos = self.cleaned_data["photos"]
        for upload in photos:
            if not is_image(upload):
                raise forms.ValidationError(f"{upload.name}: bukan file gambar yang valid.")
        return photos


class MerchantReviewForm(StyledModelForm):
    class Meta:
        model = ProductReview
        fields = ("is_published", "merchant_reply")
        labels = {"is_published": "Tampilkan di toko", "merchant_reply": "Balasan toko"}
        widgets = {"merchant_reply": forms.Textarea(attrs={"rows": 4})}


class StorePageForm(StyledModelForm):
    class Meta:
        model = StorePage
        exclude = ("store", "updated_at")
        help_texts = {"slug": "Alamat halaman, misalnya tentang-kami → /pages/tentang-kami/.",
            "body": "Teks biasa; baris kosong menjadi paragraf baru."}
        widgets = {"body": forms.Textarea(attrs={"rows": 12}),
            "seo_description": forms.Textarea(attrs={"rows": 3})}

    def clean_slug(self):
        slug = self.cleaned_data["slug"]
        pages = StorePage.objects.filter(store=self.store, slug=slug).exclude(id=self.instance.id)
        if pages.exists():
            raise forms.ValidationError("Slug ini sudah dipakai halaman lain.")
        return slug


class NewsletterForm(forms.Form):
    email = forms.EmailField(max_length=254)
    consent = forms.BooleanField(error_messages={"required": "Centang persetujuan untuk berlangganan."})


class CustomerAddressForm(StyledModelForm):
    class Meta:
        model = CustomerAddress
        fields = ("label", "customer_name", "phone", "province", "city", "district", "village",
            "postal_code", "shipping_address", "delivery_notes", "is_default")
        labels = {"label": "Label (Rumah, Kantor…)", "customer_name": "Nama penerima",
            "phone": "Nomor WhatsApp", "province": "Provinsi", "city": "Kota/Kabupaten",
            "district": "Kecamatan", "village": "Kelurahan/Desa", "postal_code": "Kode pos",
            "shipping_address": "Alamat lengkap (gedung/jalan, nomor, tower & unit, RT/RW)", "delivery_notes": "Catatan kurir",
            "is_default": "Jadikan alamat utama"}
        widgets = {"shipping_address": forms.Textarea(attrs={"rows": 3})}

    def clean_phone(self):
        from .services import normalize_indonesian_phone
        try:
            return normalize_indonesian_phone(self.cleaned_data["phone"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error

    def clean_postal_code(self):
        code = self.cleaned_data["postal_code"].strip()
        if not (len(code) == 5 and code.isdigit()):
            raise forms.ValidationError("Kode pos harus 5 digit.")
        return code
