import uuid

from django.contrib.auth.models import User
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    RegexValidator,
)
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="organizations")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


hex_color = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Gunakan warna heksadesimal, misalnya #f97316.")


class Store(models.Model):
    THEME_CHOICES = [("modern", "Modern"), ("warm", "Warm")]
    FONT_CHOICES = [("sans", "Sans modern"), ("serif", "Serif elegan"), ("rounded", "Rounded ramah")]
    FONT_STACKS = {
        "sans": "Inter,ui-sans-serif,system-ui,sans-serif",
        "serif": "ui-serif,Georgia,Cambria,'Times New Roman',serif",
        "rounded": "ui-rounded,'SF Pro Rounded','Nunito','Segoe UI',system-ui,sans-serif",
    }
    LOGO_SIZES = [("small", "Kecil"), ("medium", "Sedang"), ("large", "Besar")]
    COVER_LAYOUTS = [("split", "Teks + panel info"), ("image", "Foto latar penuh"),
        ("centered", "Teks di tengah")]
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="stores")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="stores",
        null=True, blank=True)
    name = models.CharField(max_length=120)
    logo = models.FileField(upload_to="stores/logos/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Logo PNG, JPG, or WebP. Recommended square or horizontal, maximum 5 MB.")
    logo_size = models.CharField(max_length=10, choices=LOGO_SIZES, default="medium")
    show_store_name = models.BooleanField(default=True,
        help_text="Matikan jika logo sudah memuat nama toko.")
    slug = models.SlugField(unique=True)
    domain = models.CharField(max_length=253, unique=True, null=True, blank=True)
    database_alias = models.SlugField(default="default", help_text="Deployment database alias; credentials remain in environment configuration.")
    description = models.TextField(blank=True)
    theme = models.CharField(max_length=30, choices=THEME_CHOICES, default="modern")
    storefront_template = models.CharField(max_length=30, blank=True,
        help_text="Preset tampilan terakhir yang diterapkan.")
    primary_color = models.CharField(max_length=7, default="#f97316", validators=[hex_color])
    background_color = models.CharField(max_length=7, default="#fffaf5", validators=[hex_color])
    font_family = models.CharField(max_length=10, choices=FONT_CHOICES, default="sans")
    cover_layout = models.CharField(max_length=10, choices=COVER_LAYOUTS, default="split")
    cover_image = models.FileField(upload_to="stores/covers/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Foto sampul beranda. Disarankan 1920 × 900 px, maksimal 5 MB.")
    cover_overlay = models.PositiveSmallIntegerField(default=45, validators=[MaxValueValidator(90)],
        help_text="Kegelapan lapisan di atas foto sampul (0–90%) agar teks tetap terbaca.")
    hero_eyebrow = models.CharField(max_length=100, blank=True)
    hero_title = models.CharField(max_length=180, blank=True)
    hero_text = models.TextField(blank=True)
    hero_button_label = models.CharField(max_length=40, blank=True)
    catalog_eyebrow = models.CharField(max_length=40, blank=True)
    catalog_title = models.CharField(max_length=100, blank=True)
    trust_title_1 = models.CharField(max_length=80, blank=True)
    trust_text_1 = models.CharField(max_length=120, blank=True)
    trust_title_2 = models.CharField(max_length=80, blank=True)
    trust_text_2 = models.CharField(max_length=120, blank=True)
    trust_title_3 = models.CharField(max_length=80, blank=True)
    trust_text_3 = models.CharField(max_length=120, blank=True)
    announcement_text = models.CharField(max_length=160, blank=True)
    announcement_link_label = models.CharField(max_length=40, blank=True)
    announcement_link_url = models.CharField(max_length=300, blank=True)
    campaign_eyebrow = models.CharField(max_length=80, blank=True)
    campaign_title = models.CharField(max_length=160, blank=True)
    campaign_text = models.CharField(max_length=280, blank=True)
    campaign_button_label = models.CharField(max_length=40, blank=True)
    campaign_link_url = models.CharField(max_length=300, blank=True)
    currency = models.CharField(max_length=3, default="IDR")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    support_phone = models.CharField(max_length=30, blank=True)
    support_email = models.EmailField(blank=True)
    terms_url = models.URLField(blank=True)
    privacy_url = models.URLField(blank=True)
    return_policy = models.TextField(blank=True)
    loyalty_enabled = models.BooleanField(default=False)
    loyalty_points_per_step = models.PositiveIntegerField(default=1,
        help_text="Points earned for each completed spending step.")
    loyalty_spend_step = models.PositiveIntegerField(default=10000,
        help_text="IDR spend required to earn points.")
    loyalty_point_value = models.PositiveIntegerField(default=100,
        help_text="IDR discount value of one point.")
    loyalty_min_redeem = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(loyalty_points_per_step__gt=0),
                name="store_loyalty_earn_positive"),
            models.CheckConstraint(condition=models.Q(loyalty_spend_step__gt=0),
                name="store_loyalty_step_positive"),
            models.CheckConstraint(condition=models.Q(loyalty_point_value__gt=0),
                name="store_loyalty_value_positive"),
        ]

    def __str__(self): return self.name

    @property
    def font_stack(self):
        return self.FONT_STACKS.get(self.font_family, self.FONT_STACKS["sans"])



class MarketingCampaign(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="marketing_campaigns")
    name = models.CharField(max_length=120, help_text="Internal campaign name.")
    headline = models.CharField(max_length=160)
    message = models.CharField(max_length=280, blank=True)
    coupon_code = models.CharField(max_length=50, blank=True, help_text="Optional voucher code shown to shoppers.")
    button_label = models.CharField(max_length=40, blank=True)
    link_url = models.CharField(max_length=300, blank=True)
    popup_image = models.FileField(upload_to="stores/campaigns/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])
    show_countdown = models.BooleanField(default=True)
    show_popup = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    position = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "-starts_at", "id"]

    def __str__(self):
        return self.name


class StorefrontSlide(models.Model):
    ALIGNMENTS = [("left", "Left"), ("center", "Center"), ("right", "Right")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="storefront_slides")
    image = models.FileField(upload_to="stores/slides/%Y/%m/",
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Desktop image. Recommended 1920 × 760 px, maximum 5 MB.")
    mobile_image = models.FileField(upload_to="stores/slides/mobile/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])],
        help_text="Optional mobile crop. Recommended 900 × 1100 px.")
    eyebrow = models.CharField(max_length=80, blank=True)
    headline = models.CharField(max_length=180, blank=True)
    subheadline = models.CharField(max_length=280, blank=True)
    button_label = models.CharField(max_length=40, blank=True)
    link_url = models.CharField(max_length=300, blank=True,
        help_text="Use /#catalog, /cart/, or a complete https:// URL.")
    text_alignment = models.CharField(max_length=10, choices=ALIGNMENTS, default="left")
    position = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.headline or f"Slide {self.position + 1}"


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


class StorePage(models.Model):
    """Brand content such as About, FAQ, size guide or shipping info, served at /pages/<slug>/.
    The body is plain text rendered with line breaks, so merchant input can never inject HTML."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="pages")
    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    body = models.TextField()
    seo_description = models.CharField(max_length=320, blank=True)
    show_in_header = models.BooleanField(default=False)
    show_in_footer = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [models.UniqueConstraint(fields=["store", "slug"], name="unique_store_page_slug")]

    def __str__(self):
        return self.title


class NewsletterSubscriber(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="newsletter_subscribers")
    email = models.EmailField()
    source = models.CharField(max_length=40, default="footer")
    unsubscribe_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    subscribed_at = models.DateTimeField(auto_now_add=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "email"], name="unique_store_subscriber")]

    def __str__(self):
        return self.email

    @property
    def is_subscribed(self):
        return self.unsubscribed_at is None
