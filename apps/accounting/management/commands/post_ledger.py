from django.core.management.base import BaseCommand

from apps.accounting.ledger import post_everything


class Command(BaseCommand):
    help = ("Post everything missing from the ledger: succeeded payments and refunds, and the "
        "invoices, bills and payments each module registered (backfill).")

    def handle(self, *args, **options):
        total = post_everything()
        self.stdout.write(f"Posted {total} journal entr{'y' if total == 1 else 'ies'}.")
