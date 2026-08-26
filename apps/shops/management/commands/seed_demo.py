from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.catalog.models import Category, InventoryLevel, Product, Warehouse
from apps.orders.models import Coupon, ShippingMethod
from apps.shops.models import Organization, Store


class Command(BaseCommand):
    help = "Create an idempotent demo merchant, shop, and catalog"
    def handle(self, *args, **options):
        owner, _ = User.objects.get_or_create(username="merchant", defaults={"email": "merchant@example.com", "is_staff": True})
        owner.set_password("merchant123")
        owner.save()
        organization, _ = Organization.objects.update_or_create(slug="patuhdata", defaults={"owner": owner, "name": "PatuhData Group"})
        store, _ = Store.objects.update_or_create(slug="kopi-kita", defaults={"owner": owner, "organization": organization, "name": "Kopi Kita", "description": "Kopi Nusantara, dikirim segar dari roastery kami.", "primary_color": "#b45309", "support_phone": "081234567890", "support_email": "halo@kopikita.test", "return_policy": "Pengembalian dapat diajukan setelah pesanan diterima."})
        category, _ = Category.objects.get_or_create(store=store, slug="kopi", defaults={"name": "Kopi"})
        jakarta, _ = Warehouse.objects.get_or_create(store=store, code="JKT", defaults={"name": "Gudang Jakarta", "priority": 10})
        surabaya, _ = Warehouse.objects.get_or_create(store=store, code="SBY", defaults={"name": "Gudang Surabaya", "priority": 20})
        samples = [("Gayo Natural", "GAYO-250", 89000, 24), ("Toraja Washed", "TOR-250", 99000, 18), ("Bali Kintamani", "BALI-250", 85000, 31)]
        for name, sku, price, stock in samples:
            product, _ = Product.objects.update_or_create(store=store, sku=sku, defaults={"category": category, "name": name, "slug": sku.lower(), "price": price, "weight_grams": 250, "description": "Biji kopi pilihan 250 gram, disangrai sesuai pesanan."})
            InventoryLevel.objects.update_or_create(warehouse=jakarta, product=product, defaults={"on_hand": stock, "safety_stock": 2})
            InventoryLevel.objects.update_or_create(warehouse=surabaya, product=product, defaults={"on_hand": stock // 2, "safety_stock": 1})
        ShippingMethod.objects.update_or_create(store=store, code="reguler", defaults={"name": "Pengiriman Reguler", "provider": "demo-courier", "flat_rate": 18000, "free_above": 250000, "eta": "2–4 hari kerja"})
        ShippingMethod.objects.update_or_create(store=store, code="express", defaults={"name": "Pengiriman Express", "provider": "demo-courier", "flat_rate": 35000, "eta": "1–2 hari kerja"})
        Coupon.objects.update_or_create(store=store, code="HEMAT10", defaults={"kind": "percent", "value": 10, "minimum_spend": 100000, "is_active": True})
        self.stdout.write(self.style.SUCCESS("Demo ready: merchant / merchant123"))
