from django.core.management.base import BaseCommand

from apps.tax.recurring import run_due


class Command(BaseCommand):
    help = "Issue the recurring invoices that have come due. Safe to run any number of times."

    def handle(self, *args, **options):
        issued = run_due()
        self.stdout.write(f"{len(issued)} invoice berulang diterbitkan.")
