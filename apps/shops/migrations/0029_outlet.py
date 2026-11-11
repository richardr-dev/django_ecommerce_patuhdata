from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("shops", "0028_store_is_open")]
    operations = [
        migrations.CreateModel(
            name="Outlet",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("address", models.TextField(blank=True)),
                ("phone", models.CharField(blank=True, max_length=30)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="outlets", to="shops.store")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="outlet",
            constraint=models.UniqueConstraint(fields=["store", "name"], name="unique_store_outlet_name"),
        ),
    ]
