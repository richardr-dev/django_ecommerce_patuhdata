from django.contrib import admin

from .models import (
    Coupon,
    InventoryAllocation,
    Order,
    OrderItem,
    OrderStatusEvent,
    PaymentEvent,
    PaymentTransaction,
    Refund,
    ReturnRequest,
    ShippingMethod,
)


class OrderItemInline(admin.TabularInline): model = OrderItem; extra = 0; readonly_fields = ("product", "name", "sku", "unit_price", "quantity", "line_total")
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "store", "customer_name", "status", "total", "created_at")
    list_filter = ("store", "status")
    readonly_fields = ("status", "payment_reference", "payment_checkout_url")
    inlines = [OrderItemInline]
admin.site.register(PaymentEvent)
admin.site.register(PaymentTransaction)
admin.site.register(Refund)
admin.site.register(InventoryAllocation)
admin.site.register(ShippingMethod)
admin.site.register(Coupon)
admin.site.register(OrderStatusEvent)
admin.site.register(ReturnRequest)
