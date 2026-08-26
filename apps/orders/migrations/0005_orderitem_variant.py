from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_product_height_cm_product_length_cm_and_more"), ("orders", "0004_order_destination_area_id_order_external_shipping_id")]
    operations = [migrations.AddField(
        model_name="orderitem", name="variant",
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="catalog.productvariant"),
    )]
