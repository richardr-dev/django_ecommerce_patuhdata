from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shops", "0027_customer_company_npwp_address"),
    ]

    operations = [
        migrations.AddField(
            model_name="store",
            name="is_open",
            field=models.BooleanField(
                default=True,
                help_text="Toko aktif buka. Matikan untuk menampilkan halaman 'toko sedang tutup' di storefront.",
            ),
        ),
    ]
