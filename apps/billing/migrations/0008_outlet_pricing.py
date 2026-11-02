from django.db import migrations

# Multi-outlet is a Pro feature at Rp399.000 per outlet: Pro includes one, each extra outlet
# adds Rp399.000 a month. Starter stays single-outlet; Custom is negotiated, so unlimited here.
PLANS = {
    "free": {"included_outlets": 1, "extra_outlet_price": None},
    "pro": {"included_outlets": 1, "extra_outlet_price": 399000,
        "features": "Semua fitur Starter\nProduk & toko marketplace tanpa batas\nMulti-outlet: "
            "Rp399.000 per outlet tambahan\nRekonsiliasi pencairan gateway & marketplace\n"
            "Laporan laba rugi dengan HPP\nBantuan prioritas & onboarding"},
    "custom": {"included_outlets": None, "extra_outlet_price": None,
        "features": "Semua fitur Pro\nOutlet & brand tanpa batas dengan harga khusus\n"
            "Integrasi ERP & software akuntansi\nSLA & manajer akun khusus\nKontrak & faktur "
            "perusahaan"},
    "growth": {"included_outlets": 1, "extra_outlet_price": None},
    "business": {"included_outlets": 1, "extra_outlet_price": None},
}


def apply(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for code, values in PLANS.items():
        Plan.objects.filter(code=code).update(**values)


class Migration(migrations.Migration):
    dependencies = [("billing", "0007_outlets")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
