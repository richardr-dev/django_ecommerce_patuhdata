from django.core.management.base import BaseCommand

from apps.accounting.ledger import post_pending_entries


class Command(BaseCommand):
    help = "Post every succeeded payment and refund that has no journal entry yet (backfill)."

    def handle(self, *args, **options):
        total = 0
        while posted := post_pending_entries(limit=500):
            total += posted
        self.stdout.write(f"Posted {total} journal entr{'y' if total == 1 else 'ies'}.")
