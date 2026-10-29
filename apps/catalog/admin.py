from django.contrib import admin

from .models import (
    Category,
    InventoryLevel,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Warehouse,
    WishlistItem,
)

admin.site.register(Category)
admin.site.register(Warehouse)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "store", "price", "is_active", "is_featured")
    list_filter = ("store", "is_active", "is_featured")
    search_fields = ("name", "sku")
    list_select_related = ("store",)
    raw_id_fields = ("category",)
    show_full_result_count = False


@admin.register(InventoryLevel)
class InventoryLevelAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "on_hand", "reserved", "safety_stock")
    search_fields = ("product__name", "product__sku")
    list_select_related = ("product", "warehouse")
    raw_id_fields = ("product",)


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("product", "alt_text", "position")
    search_fields = ("product__name", "product__sku")
    list_select_related = ("product",)
    raw_id_fields = ("product",)


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("product", "name", "sku", "stock", "reserved", "is_active")
    search_fields = ("name", "sku", "product__name", "product__sku")
    list_select_related = ("product",)
    raw_id_fields = ("product",)


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "rating", "display_name", "store", "is_published", "created_at")
    list_filter = ("store", "is_published", "rating")
    search_fields = ("product__name", "body")
    raw_id_fields = ("product", "user", "order")


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    list_display = ("product", "user", "store", "created_at")
    raw_id_fields = ("product", "user")
