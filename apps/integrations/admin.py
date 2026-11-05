from django.contrib import admin
from django.utils import timezone

from .models import IntegrationCredential, OutboxEvent, PlatformPayout, WhatsAppMessage

admin.site.register(IntegrationCredential)
@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("topic", "store", "status", "attempts", "next_retry_at", "created_at")
    list_filter = ("status", "topic", "store")
    search_fields = ("aggregate_id", "last_error")
    readonly_fields = ("payload", "response", "attempts", "last_attempt_at", "processed_at", "created_at")


@admin.register(PlatformPayout)
class PlatformPayoutAdmin(admin.ModelAdmin):
    """DOKU Lakuin payouts: transfer the amount to the bank account shown, then fill in the
    transfer reference and set the status to "Sudah ditransfer"."""
    list_display = ("store", "amount", "status", "bank_name", "bank_account_number",
        "bank_account_holder", "requested_at", "paid_at")
    list_filter = ("status",)
    search_fields = ("store__name", "store__slug", "transfer_reference")
    readonly_fields = ("store", "amount", "bank_name", "bank_account_number", "bank_account_holder",
        "requested_by", "requested_at", "paid_at")
    fields = ("store", "amount", "bank_name", "bank_account_number", "bank_account_holder",
        "status", "transfer_reference", "note", "requested_by", "requested_at", "paid_at")

    def has_add_permission(self, request):
        return False  # sellers request payouts from the back office

    def save_model(self, request, obj, form, change):
        if obj.status == "paid" and obj.paid_at is None:
            obj.paid_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("store", "kind", "to", "status", "provider", "created_at")
    list_filter = ("status", "kind", "provider")
    search_fields = ("store__name", "to", "error")
    readonly_fields = [field.name for field in WhatsAppMessage._meta.fields]
