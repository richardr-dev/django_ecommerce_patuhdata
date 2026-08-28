import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("catalog", "0007_flashsaleproduct"),
        ("orders", "0012_orderitem_flash_sale_offer"),
    ]

    operations = [
        migrations.CreateModel(
            name="CommerceSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("visitor_id", models.UUIDField(default=uuid.uuid4)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("last_path", models.CharField(blank=True, max_length=300)),
                ("product_view_count", models.PositiveIntegerField(default=0)),
                ("checkout_started_count", models.PositiveIntegerField(default=0)),
                ("cart_item_count", models.PositiveIntegerField(default=0)),
                ("cart_value", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("cart_snapshot", models.JSONField(default=list)),
                ("converted_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="commerce_sessions", to=settings.AUTH_USER_MODEL)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="commerce_sessions", to="shops.store")),
            ],
        ),
        migrations.CreateModel(
            name="JourneyEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("home_view", "Home view"), ("search", "Search"), ("product_view", "Product view"), ("cart_add", "Cart add"), ("cart_update", "Cart update"), ("checkout_started", "Checkout started"), ("converted", "Converted")], max_length=30)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="catalog.product")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="orders.commercesession")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(model_name="commercesession", constraint=models.UniqueConstraint(fields=("store", "visitor_id"), name="unique_store_visitor_session")),
        migrations.AddIndex(model_name="commercesession", index=models.Index(fields=["store", "last_seen_at"], name="orders_comm_store_i_d65ad6_idx")),
        migrations.AddIndex(model_name="commercesession", index=models.Index(fields=["store", "converted_at"], name="orders_comm_store_i_4944ac_idx")),
        migrations.AddIndex(model_name="journeyevent", index=models.Index(fields=["session", "kind", "created_at"], name="orders_jour_session_73a50d_idx")),
    ]
