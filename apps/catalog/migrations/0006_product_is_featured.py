from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0005_productvariant_barcode_productvariant_reserved_and_more")]

    operations = [
        migrations.AddField(model_name="product", name="is_featured", field=models.BooleanField(default=False,
            help_text="Show this product in the featured section on the homepage.")),
    ]
