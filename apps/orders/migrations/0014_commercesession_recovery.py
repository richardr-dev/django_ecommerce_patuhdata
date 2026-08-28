import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0013_commercesession_journeyevent")]

    operations = [
        migrations.AddField(model_name="commercesession", name="recovery_token",
            field=models.UUIDField(default=uuid.uuid4, unique=True)),
        migrations.AddField(model_name="commercesession", name="recovery_email",
            field=models.EmailField(blank=True, max_length=254)),
        migrations.AddField(model_name="commercesession", name="recovery_consent_at",
            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="commercesession", name="recovery_sent_at",
            field=models.DateTimeField(blank=True, null=True)),
    ]
