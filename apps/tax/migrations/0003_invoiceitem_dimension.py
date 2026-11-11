from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tax", "0002_invoice_items_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoiceitem",
            name="dimension",
            field=models.CharField(
                blank=True,
                help_text="Ukuran/dimensi, mis. 100×200cm",
                max_length=60,
                default="",
            ),
            preserve_default=False,
        ),
    ]
