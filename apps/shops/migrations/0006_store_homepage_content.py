from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0005_store_logo")]

    operations = [
        migrations.AddField(model_name="store", name="hero_eyebrow", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="store", name="hero_title", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="store", name="hero_text", field=models.TextField(blank=True)),
        migrations.AddField(model_name="store", name="hero_button_label", field=models.CharField(blank=True, max_length=40)),
        migrations.AddField(model_name="store", name="catalog_eyebrow", field=models.CharField(blank=True, max_length=40)),
        migrations.AddField(model_name="store", name="catalog_title", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="store", name="trust_title_1", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="store", name="trust_text_1", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="store", name="trust_title_2", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="store", name="trust_text_2", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="store", name="trust_title_3", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="store", name="trust_text_3", field=models.CharField(blank=True, max_length=120)),
    ]
