from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from apps.catalog.models import (
    Category,
    FlashSaleProduct,
    InventoryLevel,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Warehouse,
)
from apps.shops.models import MarketingCampaign, Store, StorefrontSlide, StorePage

from .models import Coupon, ShippingMethod


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
        if self.instance.pk:
            self.fields.pop("warehouse")
            self.fields.pop("initial_stock")


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
    class Meta: model = Warehouse; exclude = ("store",)


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


class VariantForm(ProductSkuFormMixin, StyledModelForm):
    product = ProductSkuField()

    class Meta: model = ProductVariant; fields = ("product", "name", "sku", "barcode", "attributes", "price", "weight_grams", "stock", "is_active")


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
            "primary_color", "background_color", "font_family", "cover_layout", "cover_image",
            "cover_overlay",
            "hero_eyebrow", "hero_title", "hero_text", "hero_button_label", "catalog_eyebrow",
            "catalog_title", "trust_title_1", "trust_text_1", "trust_title_2", "trust_text_2",
            "trust_title_3", "trust_text_3", "announcement_text", "announcement_link_label",
            "announcement_link_url", "campaign_eyebrow", "campaign_title", "campaign_text",
            "campaign_button_label", "campaign_link_url", "currency", "tax_rate",
            "support_phone", "support_email", "terms_url", "privacy_url", "return_policy")
        fields = fields + ("loyalty_enabled", "loyalty_points_per_step", "loyalty_spend_step",
            "loyalty_point_value", "loyalty_min_redeem")
        widgets = {"description": forms.Textarea(attrs={"rows": 3}),
            "hero_text": forms.Textarea(attrs={"rows": 3}),
            "return_policy": forms.Textarea(attrs={"rows": 4}),
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "background_color": forms.TextInput(attrs={"type": "color"}),
            "cover_overlay": forms.NumberInput(attrs={"min": 0, "max": 90, "step": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            for name in ("loyalty_points_per_step", "loyalty_spend_step", "loyalty_point_value",
                    "loyalty_min_redeem", "logo_size", "background_color", "font_family",
                    "cover_layout", "cover_overlay"):
                if name not in self.data:
                    self.fields.pop(name)
        labels = {
            "hero_eyebrow": "Label kecil hero", "hero_title": "Judul utama",
            "hero_text": "Deskripsi hero", "hero_button_label": "Teks tombol utama",
            "catalog_eyebrow": "Label katalog", "catalog_title": "Judul katalog",
            "trust_title_1": "Keunggulan 1", "trust_text_1": "Penjelasan 1",
            "trust_title_2": "Keunggulan 2", "trust_text_2": "Penjelasan 2",
            "trust_title_3": "Keunggulan 3", "trust_text_3": "Penjelasan 3",
            "announcement_text": "Teks pengumuman", "announcement_link_label": "Teks tautan pengumuman",
            "announcement_link_url": "Tujuan pengumuman", "campaign_eyebrow": "Label kampanye",
            "campaign_title": "Judul kampanye", "campaign_text": "Deskripsi kampanye",
            "campaign_button_label": "Teks tombol kampanye", "campaign_link_url": "Tujuan kampanye"}
        for name, label in labels.items(): self.fields[name].label = label
        self.fields["domain"].help_text = "Kosongkan saat lokal. Isi tanpa https://, misalnya tokoanda.id."
        self.fields["primary_color"].help_text = "Warna tombol, tautan, dan aksen toko."
        extra_labels = {"logo_size": "Ukuran logo", "show_store_name": "Tampilkan nama di samping logo",
            "theme": "Gaya warna sampul", "background_color": "Warna latar halaman",
            "font_family": "Jenis huruf", "cover_layout": "Tata letak sampul",
            "cover_image": "Foto sampul", "cover_overlay": "Kegelapan foto sampul (%)"}
        for name, label in extra_labels.items():
            if name in self.fields:
                self.fields[name].label = label

    def clean(self):
        cleaned = super().clean()
        for name in ("announcement_link_url", "campaign_link_url"):
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

    def clean_cover_image(self):
        return self._clean_image("cover_image", "foto sampul")


class ProductReviewForm(StyledModelForm):
    rating = forms.TypedChoiceField(choices=[(n, f"{n} bintang") for n in range(5, 0, -1)],
        coerce=int, label="Penilaian")

    class Meta:
        model = ProductReview
        fields = ("rating", "title", "body")
        labels = {"title": "Judul ulasan", "body": "Ulasan Anda"}
        widgets = {"body": forms.Textarea(attrs={"rows": 4, "maxlength": 2000})}


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
