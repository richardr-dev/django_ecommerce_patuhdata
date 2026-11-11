from django.contrib import admin

from .models import ConsignmentPartner, Settlement, SettlementLine, SettlementPayment


class SettlementLineInline(admin.TabularInline):
    model = SettlementLine
    extra = 0
    readonly_fields = ("order", "sale_date", "ab_shop_source", "line_total", "shipping_contribution")


class SettlementPaymentInline(admin.TabularInline):
    model = SettlementPayment
    extra = 0
    readonly_fields = ("amount", "payment_date", "method", "reference", "recorded_by")


@admin.register(ConsignmentPartner)
class ConsignmentPartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "store", "contact_name", "contact_phone", "payment_term_days", "is_active")
    list_filter = ("store", "is_active")
    search_fields = ("name", "contact_name")


@admin.register(Settlement)
class SettlementAdmin(admin.ModelAdmin):
    list_display = ("partner", "store", "period_start", "period_end", "status",
                    "invoice_number", "total_due", "amount_paid", "balance_due")
    list_filter = ("store", "status", "partner")
    search_fields = ("invoice_number", "partner__name")
    readonly_fields = ("id", "invoice_number", "confirmed_at", "invoiced_at", "closed_at",
                       "created_at", "updated_at", "balance_due")
    inlines = [SettlementLineInline, SettlementPaymentInline]

    def balance_due(self, obj):
        return obj.balance_due
    balance_due.short_description = "Sisa tagihan"
