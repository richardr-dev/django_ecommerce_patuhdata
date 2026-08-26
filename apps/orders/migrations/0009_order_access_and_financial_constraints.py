import uuid

from django.db import migrations, models


def populate_access_tokens(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    for order in Order.objects.filter(access_token__isnull=True).iterator():
        order.access_token = uuid.uuid4()
        order.save(update_fields=["access_token"])


class Migration(migrations.Migration):
    dependencies = [("orders", "0008_paymenttransaction_provider_payment_id_refund")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="access_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_access_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="order",
            name="access_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(model_name="order", constraint=models.CheckConstraint(
            condition=models.Q(subtotal__gte=0), name="order_subtotal_nonnegative")),
        migrations.AddConstraint(model_name="order", constraint=models.CheckConstraint(
            condition=models.Q(discount__gte=0), name="order_discount_nonnegative")),
        migrations.AddConstraint(model_name="order", constraint=models.CheckConstraint(
            condition=models.Q(shipping_cost__gte=0), name="order_shipping_nonnegative")),
        migrations.AddConstraint(model_name="order", constraint=models.CheckConstraint(
            condition=models.Q(tax__gte=0), name="order_tax_nonnegative")),
        migrations.AddConstraint(model_name="order", constraint=models.CheckConstraint(
            condition=models.Q(total__gte=0), name="order_total_nonnegative")),
        migrations.AddConstraint(model_name="orderitem", constraint=models.CheckConstraint(
            condition=models.Q(quantity__gt=0), name="order_item_quantity_positive")),
        migrations.AddConstraint(model_name="orderitem", constraint=models.CheckConstraint(
            condition=models.Q(unit_price__gte=0), name="order_item_price_nonnegative")),
        migrations.AddConstraint(model_name="orderitem", constraint=models.CheckConstraint(
            condition=models.Q(line_total__gte=0), name="order_item_total_nonnegative")),
        migrations.AddConstraint(model_name="paymenttransaction", constraint=models.CheckConstraint(
            condition=models.Q(amount__gt=0), name="payment_amount_positive")),
    ]
