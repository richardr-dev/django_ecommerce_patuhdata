import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0002_store_privacy_url_store_return_policy_and_more"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="Organization", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=160)), ("slug", models.SlugField(unique=True)),
            ("is_active", models.BooleanField(default=True)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="organizations", to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.AddField(model_name="store", name="database_alias", field=models.SlugField(default="default", help_text="Deployment database alias; credentials remain in environment configuration.")),
        migrations.AddField(model_name="store", name="organization", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="stores", to="shops.organization")),
    ]
