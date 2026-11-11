from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shops", "0026_customer_uuid_nodash")]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="company_name",
            field=models.CharField(blank=True, max_length=160,
                help_text="Nama perusahaan untuk pelanggan B2B."),
        ),
        migrations.AddField(
            model_name="customer",
            name="npwp",
            field=models.CharField(blank=True, max_length=20,
                help_text="NPWP wajib pajak, dicetak di invoice."),
        ),
        migrations.AddField(
            model_name="customer",
            name="address",
            field=models.TextField(blank=True,
                help_text="Alamat tagihan / pengiriman default."),
        ),
    ]
