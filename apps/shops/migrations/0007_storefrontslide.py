import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0006_store_homepage_content")]

    operations = [
        migrations.CreateModel(
            name="StorefrontSlide",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("image", models.FileField(help_text="Desktop image. Recommended 1920 × 760 px, maximum 5 MB.", upload_to="stores/slides/%Y/%m/", validators=[django.core.validators.FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])),
                ("mobile_image", models.FileField(blank=True, help_text="Optional mobile crop. Recommended 900 × 1100 px.", upload_to="stores/slides/mobile/%Y/%m/", validators=[django.core.validators.FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])),
                ("eyebrow", models.CharField(blank=True, max_length=80)),
                ("headline", models.CharField(blank=True, max_length=180)),
                ("subheadline", models.CharField(blank=True, max_length=280)),
                ("button_label", models.CharField(blank=True, max_length=40)),
                ("link_url", models.CharField(blank=True, help_text="Use /#catalog, /cart/, or a complete https:// URL.", max_length=300)),
                ("text_alignment", models.CharField(choices=[("left", "Left"), ("center", "Center"), ("right", "Right")], default="left", max_length=10)),
                ("position", models.PositiveSmallIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="storefront_slides", to="shops.store")),
            ],
            options={"ordering": ["position", "id"]},
        ),
    ]
