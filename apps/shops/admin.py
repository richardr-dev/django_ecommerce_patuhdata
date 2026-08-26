from django.contrib import admin

from .models import AuditLog, Customer, Organization, Store, StoreStaff

admin.site.register(Organization)
admin.site.register(Store)
admin.site.register(StoreStaff)
admin.site.register(Customer)
admin.site.register(AuditLog)
