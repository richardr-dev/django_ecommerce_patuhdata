import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounting", "0003_expenses")]
    operations = [
        migrations.CreateModel(
            name="Supplier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160)),
                ("contact", models.CharField(blank=True, max_length=120)),
                ("phone", models.CharField(blank=True, max_length=30)),
                ("email", models.EmailField(blank=True)),
                ("address", models.TextField(blank=True)),
                ("npwp", models.CharField(blank=True, max_length=20)),
                ("notes", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="suppliers", to="shops.store")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="supplier",
            constraint=models.UniqueConstraint(fields=["store", "name"], name="unique_store_supplier_name"),
        ),
        migrations.CreateModel(
            name="Bill",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("number", models.CharField(blank=True, max_length=40)),
                ("status", models.CharField(
                    choices=[("draft","Draft"),("open","Belum Dibayar"),("paid","Lunas"),
                             ("partial","Bayar Sebagian"),("overdue","Jatuh Tempo"),("void","Dibatalkan")],
                    default="open", max_length=10)),
                ("bill_date", models.DateField()),
                ("due_date", models.DateField(blank=True, null=True)),
                ("subtotal", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("tax", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bills", to="shops.store")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bills", to="accounting.supplier")),
            ],
            options={"ordering": ["-bill_date", "-created_at"]},
        ),
        migrations.CreateModel(
            name="BillItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=200)),
                ("quantity", models.DecimalField(decimal_places=2, default=1, max_digits=10)),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("line_total", models.DecimalField(decimal_places=2, max_digits=14)),
                ("bill", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="accounting.bill")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="BillPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("paid_on", models.DateField()),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("method", models.CharField(
                    choices=[("transfer","Transfer Bank"),("cash","Tunai"),("other","Lainnya")],
                    default="transfer", max_length=20)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("bill", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="accounting.bill")),
                ("recorded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["paid_on", "id"]},
        ),
        migrations.AddConstraint(
            model_name="billpayment",
            constraint=models.CheckConstraint(condition=models.Q(amount__gt=0), name="bill_payment_positive"),
        ),
    ]
