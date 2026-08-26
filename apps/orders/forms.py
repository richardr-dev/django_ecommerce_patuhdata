from django import forms

from apps.catalog.models import Category, InventoryLevel, Product, ProductVariant, Warehouse
from apps.shops.models import Store

from .models import Coupon, ShippingMethod


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")


class ProductForm(StyledModelForm):
    class Meta:
        model = Product
        exclude = ("store", "created_at")
        widgets = {"description": forms.Textarea(attrs={"rows": 4}), "seo_description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(store=self.store)


class CategoryForm(StyledModelForm):
    class Meta: model = Category; exclude = ("store",)


class WarehouseForm(StyledModelForm):
    class Meta: model = Warehouse; exclude = ("store",)


class InventoryForm(StyledModelForm):
    class Meta: model = InventoryLevel; fields = ("warehouse", "product", "on_hand", "reserved", "safety_stock")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(store=self.store)
        self.fields["product"].queryset = Product.objects.filter(store=self.store)


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
