from django.db import migrations, models


def move_default_blue_to_orange(apps, schema_editor):
    Store = apps.get_model("shops", "Store")
    Store.objects.filter(primary_color="#155eef").update(primary_color="#f97316")


class Migration(migrations.Migration):
    dependencies = [("shops", "0009_marketingcampaign")]

    operations = [
        migrations.AlterField(model_name="store", name="primary_color",
            field=models.CharField(default="#f97316", max_length=7)),
        migrations.RunPython(move_default_blue_to_orange, migrations.RunPython.noop),
    ]
