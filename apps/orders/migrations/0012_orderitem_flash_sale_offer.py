import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0007_flashsaleproduct"),
        ("orders", "0011_order_fulfillment_tracking"),
    ]

    operations = [
        migrations.AddField(model_name="orderitem", name="flash_sale_offer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="order_items", to="catalog.flashsaleproduct")),
    ]
