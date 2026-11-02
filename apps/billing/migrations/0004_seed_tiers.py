from django.db import migrations

# Starter to Custom. Codes "free" and "pro" are kept so existing subscriptions carry over.
# Prices and limits stay editable in /platform-admin/billing/plan/; None means unlimited.
TIERS = [
    {"code": "free", "name": "Starter", "tagline": "Mulai jualan online tanpa biaya.",
        "price_monthly": 0, "price_yearly": 0, "max_products": 50, "max_channels": 1,
        "allow_custom_domain": False, "allow_payout_reconciliation": False,
        "contact_sales": False, "is_default": True, "sort_order": 1,
        "features": "Toko online, checkout & pengiriman\nTransfer bank & payment gateway milik "
            "sendiri\nInvoice & laporan pajak otomatis\nBantuan lewat email"},
    {"code": "growth", "name": "Growth", "tagline": "Untuk toko yang mulai ramai pesanan.",
        "price_monthly": 49000, "price_yearly": 490000, "max_products": 500, "max_channels": 3,
        "allow_custom_domain": True, "allow_payout_reconciliation": False,
        "contact_sales": False, "is_default": False, "sort_order": 2,
        "features": "Semua fitur Starter\nDomain sendiri + HTTPS otomatis\nStok terpadu dengan "
            "3 toko marketplace\nBantuan WhatsApp di jam kerja"},
    {"code": "pro", "name": "Pro", "tagline": "Produk tanpa batas dan pembukuan lengkap.",
        "price_monthly": 99000, "price_yearly": 990000, "max_products": None, "max_channels": 10,
        "allow_custom_domain": True, "allow_payout_reconciliation": True,
        "contact_sales": False, "is_default": False, "sort_order": 3,
        "features": "Semua fitur Growth\nRekonsiliasi pencairan gateway & marketplace\n"
            "Laporan laba rugi dengan HPP\nHingga 10 toko marketplace"},
    {"code": "business", "name": "Business", "tagline": "Untuk brand dengan banyak kanal.",
        "price_monthly": 299000, "price_yearly": 2990000, "max_products": None,
        "max_channels": None, "allow_custom_domain": True, "allow_payout_reconciliation": True,
        "contact_sales": False, "is_default": False, "sort_order": 4,
        "features": "Semua fitur Pro\nToko marketplace tanpa batas\nBantuan prioritas & "
            "onboarding\nBantuan migrasi katalog"},
    {"code": "custom", "name": "Custom", "tagline": "Harga dan batas sesuai kebutuhan usaha Anda.",
        "price_monthly": 0, "price_yearly": 0, "max_products": None, "max_channels": None,
        "allow_custom_domain": True, "allow_payout_reconciliation": True,
        "contact_sales": True, "is_default": False, "sort_order": 5,
        "features": "Semua fitur Business\nIntegrasi ERP & software akuntansi\nSLA & manajer "
            "akun khusus\nKontrak & faktur perusahaan"},
]


def seed(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for tier in TIERS:
        Plan.objects.update_or_create(code=tier["code"], defaults=tier)


class Migration(migrations.Migration):
    dependencies = [("billing", "0003_plan_tiers")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
