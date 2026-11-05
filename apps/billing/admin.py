from django.contrib import admin

from .models import (
    Plan,
    Referral,
    ReferralLink,
    ReferralReward,
    Subscription,
    SubscriptionInvoice,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price_monthly", "price_yearly", "max_products",
        "max_channels", "allow_custom_domain", "allow_payout_reconciliation", "contact_sales",
        "is_default", "is_public", "sort_order")


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


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("referrer", "referred", "status", "created_at", "qualified_at")
    list_filter = ("status",)
    search_fields = ("referrer__slug", "referrer__name", "referred__slug", "referred__name")
    raw_id_fields = ("referrer", "referred")


@admin.register(ReferralReward)
class ReferralRewardAdmin(admin.ModelAdmin):
    list_display = ("store", "kind", "percent", "invoice", "used_at", "created_at")
    list_filter = ("kind",)
    search_fields = ("store__slug", "store__name")
    raw_id_fields = ("store", "referral", "invoice")


@admin.register(ReferralLink)
class ReferralLinkAdmin(admin.ModelAdmin):
    list_display = ("store", "clicks", "last_clicked_at")
    search_fields = ("store__slug", "store__name")
    raw_id_fields = ("store",)
