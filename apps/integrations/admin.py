from django.contrib import admin

from .models import IntegrationCredential, OutboxEvent

admin.site.register(IntegrationCredential)
@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("topic", "store", "status", "attempts", "next_retry_at", "created_at")
    list_filter = ("status", "topic", "store")
    search_fields = ("aggregate_id", "last_error")
    readonly_fields = ("payload", "response", "attempts", "last_attempt_at", "processed_at", "created_at")
