import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0006_product_is_featured"),
        ("shops", "0009_marketingcampaign"),
    ]

    operations = [
        migrations.CreateModel(
            name="FlashSaleProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sale_price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("stock_limit", models.PositiveIntegerField(blank=True, help_text="Optional maximum units sold during this campaign.", null=True)),
                ("per_customer_limit", models.PositiveIntegerField(blank=True, help_text="Optional maximum units per customer phone number.", null=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_offers", to="shops.marketingcampaign")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="flash_sale_offers", to="catalog.product")),
            ],
        ),
        migrations.AddConstraint(model_name="flashsaleproduct", constraint=models.UniqueConstraint(fields=("campaign", "product"), name="unique_campaign_product_offer")),
        migrations.AddConstraint(model_name="flashsaleproduct", constraint=models.CheckConstraint(condition=models.Q(("sale_price__gt", 0)), name="flash_sale_price_positive")),
    ]
