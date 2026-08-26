from django.contrib.auth.models import User
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="organizations")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Store(models.Model):
    THEME_CHOICES = [("modern", "Modern"), ("warm", "Warm")]
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="stores")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="stores",
        null=True, blank=True)
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    domain = models.CharField(max_length=253, unique=True, null=True, blank=True)
    database_alias = models.SlugField(default="default", help_text="Deployment database alias; credentials remain in environment configuration.")
    description = models.TextField(blank=True)
    theme = models.CharField(max_length=30, choices=THEME_CHOICES, default="modern")
    primary_color = models.CharField(max_length=7, default="#155eef")
    currency = models.CharField(max_length=3, default="IDR")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    support_phone = models.CharField(max_length=30, blank=True)
    support_email = models.EmailField(blank=True)
    terms_url = models.URLField(blank=True)
    privacy_url = models.URLField(blank=True)
    return_policy = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.name

class StoreStaff(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="staff_members")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=[("admin", "Group/Brand Admin"),
        ("manager", "Brand Manager"), ("warehouse", "Warehouse"), ("finance", "Finance"),
        ("customer_service", "Customer Service"), ("ops", "Operations"), ("viewer", "Viewer")])
    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "user"], name="unique_store_staff")]


class Customer(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="customers")
    name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30)
    accepts_marketing = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "phone"], name="unique_store_customer_phone")]


class AuditLog(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
