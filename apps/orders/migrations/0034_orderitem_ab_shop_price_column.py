from django.db import migrations, models


def add_missing_column(apps, schema_editor):
    """0030 added OrderItem.ab_shop_price to the migration state only, assuming the column was
    already there. On every database built from migrations (tests, new deployments) it is not,
    so create it wherever it is missing."""
    OrderItem = apps.get_model("orders", "OrderItem")
    table = OrderItem._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        columns = {column.name for column in
            schema_editor.connection.introspection.get_table_description(cursor, table)}
    if "ab_shop_price" in columns:
        return
    field = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    field.set_attributes_from_name("ab_shop_price")
    schema_editor.add_field(OrderItem, field)


class Migration(migrations.Migration):
    dependencies = [("orders", "0033_order_customer_fk")]
    operations = [migrations.RunPython(add_missing_column, migrations.RunPython.noop)]
