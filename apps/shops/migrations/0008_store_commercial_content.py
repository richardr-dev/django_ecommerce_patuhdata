from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0007_storefrontslide")]

    operations = [
        migrations.AddField(model_name="store", name="announcement_text", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="store", name="announcement_link_label", field=models.CharField(blank=True, max_length=40)),
        migrations.AddField(model_name="store", name="announcement_link_url", field=models.CharField(blank=True, max_length=300)),
        migrations.AddField(model_name="store", name="campaign_eyebrow", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="store", name="campaign_title", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="store", name="campaign_text", field=models.CharField(blank=True, max_length=280)),
        migrations.AddField(model_name="store", name="campaign_button_label", field=models.CharField(blank=True, max_length=40)),
        migrations.AddField(model_name="store", name="campaign_link_url", field=models.CharField(blank=True, max_length=300)),
    ]
