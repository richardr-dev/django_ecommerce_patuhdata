from django.db import migrations

# Three plans on sale: Starter (paid, with a free trial at signup), Pro, and Custom. Growth and
# Business stay in the table, hidden, so any store already on them keeps working.
PLANS = {
    "free": {"name": "Starter", "tagline": "Semua yang dibutuhkan untuk mulai jualan online.",
        "price_monthly": 99000, "price_yearly": 990000, "max_products": 500, "max_channels": 2,
        "allow_custom_domain": True, "allow_payout_reconciliation": False, "is_public": True,
        "is_default": True, "sort_order": 1,
        "features": "Toko online + domain sendiri\nTransfer bank & payment gateway milik "
            "sendiri\nOngkir, resi & label pengiriman\nInvoice & laporan pajak otomatis\n"
            "Stok terpadu dengan 2 toko marketplace\nBantuan via WhatsApp"},
    "pro": {"name": "Pro", "tagline": "Untuk brand yang berjualan di banyak kanal.",
        "price_monthly": 399000, "price_yearly": 3990000, "max_products": None,
        "max_channels": None, "allow_custom_domain": True, "allow_payout_reconciliation": True,
        "is_public": True, "sort_order": 2,
        "features": "Semua fitur Starter\nProduk & toko marketplace tanpa batas\nRekonsiliasi "
            "pencairan gateway & marketplace\nLaporan laba rugi dengan HPP\nBantuan prioritas & "
            "onboarding"},
    "custom": {"sort_order": 3, "is_public": True},
    "growth": {"is_public": False},
    "business": {"is_public": False},
}


def apply(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for code, values in PLANS.items():
        Plan.objects.filter(code=code).update(**values)


class Migration(migrations.Migration):
    dependencies = [("billing", "0004_seed_tiers")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
