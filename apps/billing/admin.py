from django.contrib import admin

from .models import Plan, Subscription, SubscriptionInvoice


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price_monthly", "price_yearly", "max_products",
        "allow_custom_domain", "is_default", "is_public")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("store", "plan", "current_period_end", "updated_at")
    list_filter = ("plan",)
    search_fields = ("store__name", "store__slug")


@admin.register(SubscriptionInvoice)
class SubscriptionInvoiceAdmin(admin.ModelAdmin):
    list_display = ("store", "plan", "months", "amount", "status", "provider", "created_at", "paid_at")
    list_filter = ("status", "plan", "provider")
    search_fields = ("store__name", "payment_reference")
    readonly_fields = ("id", "payment_reference", "checkout_url", "paid_at", "created_at")
