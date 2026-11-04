from django.contrib import admin
from django.utils import timezone

from .models import (
    AuditLog,
    Customer,
    NewsletterSubscriber,
    Organization,
    Store,
    StorePage,
    StoreStaff,
)

admin.site.register(Organization)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "verified_at", "is_active", "created_at")
    list_filter = ("is_active", ("verified_at", admin.EmptyFieldListFilter))
    search_fields = ("name", "slug", "owner__username", "owner__email")
    actions = ("mark_verified", "clear_verified")

    @admin.action(description="Tandai terverifikasi (identitas usaha sudah dicek)")
    def mark_verified(self, request, queryset):
        updated = queryset.filter(verified_at__isnull=True).update(verified_at=timezone.now())
        self.message_user(request, f"{updated} toko ditandai terverifikasi.")

    @admin.action(description="Cabut status terverifikasi")
    def clear_verified(self, request, queryset):
        self.message_user(request, f"{queryset.update(verified_at=None)} toko dicabut verifikasinya.")


admin.site.register(StoreStaff)
admin.site.register(Customer)
admin.site.register(AuditLog)
admin.site.register(StorePage)
admin.site.register(NewsletterSubscriber)
