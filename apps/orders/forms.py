from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from apps.catalog.models import (
    Category,
    InventoryLevel,
    Product,
    ProductImage,
    ProductVariant,
    Warehouse,
)
from apps.shops.models import Store

from .models import Coupon, ShippingMethod


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")


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
        self.fields["category"].queryset = Category.objects.filter(store=self.store)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(store=self.store, is_active=True)
        if self.instance.pk:
            self.fields.pop("warehouse")
            self.fields.pop("initial_stock")


class ProductImageForm(StyledModelForm):
    class Meta:
        model = ProductImage
        fields = ("product", "file", "url", "alt_text", "position")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(store=self.store)

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


class InventoryForm(StyledModelForm):
    class Meta: model = InventoryLevel; fields = ("warehouse", "product", "on_hand", "safety_stock")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(store=self.store)
        self.fields["product"].queryset = Product.objects.filter(store=self.store)
        self.fields["on_hand"].help_text = "Jumlah fisik di gudang. Stok yang sedang dipesan tetap terlindungi."
        self.fields["safety_stock"].help_text = "Jumlah minimum yang tidak ditawarkan ke pelanggan."
        if self.instance.pk:
            self.fields["warehouse"].disabled = True
            self.fields["product"].disabled = True


class VariantForm(StyledModelForm):
    class Meta: model = ProductVariant; fields = ("product", "name", "sku", "barcode", "attributes", "price", "weight_grams", "stock", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(store=self.store)


class ShippingMethodForm(StyledModelForm):
    class Meta: model = ShippingMethod; exclude = ("store",)


class CouponForm(StyledModelForm):
    class Meta: model = Coupon; exclude = ("store", "used_count")


class StoreSettingsForm(StyledModelForm):
    class Meta:
        model = Store
        fields = ("name", "domain", "description", "theme", "primary_color", "currency", "tax_rate",
            "support_phone", "support_email", "terms_url", "privacy_url", "return_policy")
        widgets = {"description": forms.Textarea(attrs={"rows": 3}), "return_policy": forms.Textarea(attrs={"rows": 4})}
