from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.orders.setup_views import MAX_IMPORT_ROWS, import_products, parse_import_rows
from apps.shops.models import Store


class Command(BaseCommand):
    help = "Load a synthetic catalog (default 10,000 SKUs) into a store to test catalog scale"

    def add_arguments(self, parser):
        parser.add_argument("--store", default="kopi-kita", help="Store slug")
        parser.add_argument("--count", type=int, default=10000)
        parser.add_argument("--categories", type=int, default=40)

    def handle(self, *args, store, count, categories, **options):
        store_obj = Store.objects.filter(slug=store).first()
        if not store_obj:
            raise CommandError(f"Store {store!r} not found. Run seed_demo first.")
        created = updated = 0
        for start in range(0, count, MAX_IMPORT_ROWS):
            rows = [{"name": f"Produk Uji {number:05d}", "sku": f"LOAD-{number:05d}",
                "price": str(10000 + (number * 137) % 490000), "stock": str(number % 50),
                "category": f"Kategori {number % categories + 1:02d}", "weight_grams": "250",
                "description": "Produk sintetis untuk uji skala katalog."}
                for number in range(start + 1, min(start + MAX_IMPORT_ROWS, count) + 1)]
            with transaction.atomic():
                batch_created, batch_updated = import_products(store_obj, parse_import_rows(rows))
            created, updated = created + batch_created, updated + batch_updated
        self.stdout.write(self.style.SUCCESS(
            f"{store_obj.name}: {created} products created, {updated} updated."))
