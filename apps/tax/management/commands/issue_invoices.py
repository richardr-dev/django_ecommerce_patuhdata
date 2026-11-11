from django.core.management.base import BaseCommand

from apps.orders.models import Order
from apps.tax.services import issue_invoice_for_order


class Command(BaseCommand):
    help = "Issue invoices for all orders that do not have one yet."

    def add_arguments(self, parser):
        parser.add_argument("--store", type=int, help="Limit to store ID")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        qs = Order.objects.filter(invoice__isnull=True).select_related("store")
        if options["store"]:
            qs = qs.filter(store_id=options["store"])
        issued = skipped = errors = 0
        for order in qs:
            if options["dry_run"]:
                self.stdout.write(f"  would issue: {order.id}")
                issued += 1
                continue
            try:
                inv, created = issue_invoice_for_order(order)
                if created:
                    issued += 1
                    self.stdout.write(f"  issued {inv.number} for order {order.id}")
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f"  FAIL order {order.id}: {exc}")
        self.stdout.write(self.style.SUCCESS(
            f"Done: {issued} issued, {skipped} skipped, {errors} errors"))
