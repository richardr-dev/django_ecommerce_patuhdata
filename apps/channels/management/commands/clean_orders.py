from django.core.management.base import BaseCommand

from apps.channels.cleanup import clean
from apps.shops.models import Store


class Command(BaseCommand):
    help = ("Report (and with --apply, remove) leftovers of imports: empty import batches. "
        "Duplicate imports and look-alike orders are only listed, never deleted.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Remove the empty import batches.")
        parser.add_argument("--store", help="Store slug; default is every store.")

    def handle(self, *args, **options):
        stores = Store.objects.filter(slug=options["store"]) if options["store"] else Store.objects.all()
        for store in stores:
            report = clean(store, apply=options["apply"])
            self.stdout.write(f"\n{store.slug}")
            verb = "removed" if options["apply"] else "would remove"
            self.stdout.write(f"  empty import batches: {report['empty_batches']}"
                f" ({verb} {report['removed'] if options['apply'] else report['empty_batches']})")
            self.stdout.write(f"  duplicate imported orders: {len(report['duplicates'])}")
            for dup in report["duplicates"]:
                self.stdout.write(f"    {dup['channel']} {dup['external_order_id']} x{dup['count']}")
            self.stdout.write(f"  look-alike orders (same buyer, total, day): {len(report['look_alikes'])}")
            for row in report["look_alikes"]:
                self.stdout.write(f"    {row['day']} {row['name']} Rp{row['total']:,.0f} x{row['count']}")
