from django.db import migrations

# Starting tiers; prices and limits stay editable in /platform-admin/billing/plan/.
PLANS = [
    {"code": "free", "name": "Gratis", "tagline": "Mulai jualan tanpa biaya.",
        "price_monthly": 0, "price_yearly": 0, "max_products": 50,
        "allow_custom_domain": False, "is_default": True, "sort_order": 1},
    {"code": "pro", "name": "Pro", "tagline": "Produk tanpa batas dan domain sendiri.",
        "price_monthly": 99000, "price_yearly": 990000, "max_products": None,
        "allow_custom_domain": True, "is_default": False, "sort_order": 2},
]


def seed(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for plan in PLANS:
        Plan.objects.get_or_create(code=plan["code"], defaults=plan)


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
