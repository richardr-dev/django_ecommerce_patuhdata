import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0008_store_commercial_content")]

    operations = [
        migrations.CreateModel(
            name="MarketingCampaign",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Internal campaign name.", max_length=120)),
                ("headline", models.CharField(max_length=160)),
                ("message", models.CharField(blank=True, max_length=280)),
                ("coupon_code", models.CharField(blank=True, help_text="Optional voucher code shown to shoppers.", max_length=50)),
                ("button_label", models.CharField(blank=True, max_length=40)),
                ("link_url", models.CharField(blank=True, max_length=300)),
                ("popup_image", models.FileField(blank=True, upload_to="stores/campaigns/%Y/%m/", validators=[django.core.validators.FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])),
                ("show_countdown", models.BooleanField(default=True)),
                ("show_popup", models.BooleanField(default=False)),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                ("position", models.PositiveSmallIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="marketing_campaigns", to="shops.store")),
            ],
            options={"ordering": ["position", "-starts_at", "id"]},
        ),
    ]
