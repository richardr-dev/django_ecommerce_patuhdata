from django.core.management.base import BaseCommand

from apps.orders.stock import record_opening_balances, stock_audit
from apps.shops.models import Store


class Command(BaseCommand):
    help = ("Check that every stock figure equals the sum of its movements. With --fix, add an "
        "opening-balance movement for stock that predates movement tracking.")

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true")

    def handle(self, *args, **options):
        for store in Store.objects.all():
            drift = stock_audit(store)
            if not drift:
                continue
            self.stdout.write(f"{store.name}: {len(drift)} item tidak cocok dengan pergerakannya")
            for item, recorded, explained in drift[:20]:
                self.stdout.write(f"  {item}: stok {recorded}, pergerakan {explained}")
            if options["fix"]:
                self.stdout.write(f"  {record_opening_balances(store)} saldo awal dicatat")
