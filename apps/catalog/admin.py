from django.contrib import admin

from .models import Category, InventoryLevel, Product, ProductImage, ProductVariant, Warehouse

admin.site.register(Category)
admin.site.register(Product)
admin.site.register(Warehouse)
admin.site.register(InventoryLevel)
admin.site.register(ProductImage)
admin.site.register(ProductVariant)
