from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounting import adjustments
from apps.accounting.assets import depreciate
from apps.accounting.ledger import post_everything
from apps.accounting.models import FixedAsset
from apps.shops.models import Store
from apps.tax.recurring import run_due


class Command(BaseCommand):
    help = ("The books' daily worker: posts documents missing from the ledger, issues due "
        "recurring invoices, and books depreciation up to the end of last month. Safe to run "
        "any number of times.")

    def handle(self, *args, **options):
        today = timezone.localdate()
        posted = post_everything()
        issued = len(run_due(today))
        last_month_end = today.replace(day=1) - timedelta(days=1)
        depreciated = 0
        for store in Store.objects.filter(fixed_assets__status=FixedAsset.ACTIVE).distinct():
            depreciated += len(depreciate(store, through=last_month_end))
        adjusted = 0
        for store in Store.objects.all():
            adjusted += len(adjustments.amortize(store, through=last_month_end))
            adjusted += len(adjustments.run_templates(store, today=today))
        self.stdout.write(f"{adjusted} jurnal penyesuaian (biaya dibayar di muka, akrual, berulang) dicatat.")
        self.stdout.write(f"{posted} jurnal diposting, {issued} invoice berulang diterbitkan, "
            f"{depreciated} jurnal penyusutan dicatat.")
