import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0032_mitra_partner_fk"),
        ("shops", "0026_customer_uuid_nodash"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="customer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="orders",
                to="shops.customer",
                help_text="Linked CRM customer.",
            ),
        ),
    ]
