"""SPT Masa PPN worksheet: figures tie to the ledger and overpayments carry forward."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.ledger import INPUT_VAT, VAT, post_entry
from apps.tax import spt


class SptTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def post(self, day, keluaran=0, masukan=0):
        # Dr cash 1100 against PPN keluaran / masukan, one document each
        if keluaran:
            post_entry(self.store, date=day, memo="jual", source=("test_out", day.toordinal()),
                amounts={"1100": Decimal(keluaran), VAT: -Decimal(keluaran)})
        if masukan:
            post_entry(self.store, date=day, memo="beli", source=("test_in", day.toordinal()),
                amounts={INPUT_VAT: Decimal(masukan), "1100": -Decimal(masukan)})

    def test_net_owed(self):
        d = datetime.date(2026, 3, 10)
        self.post(d, keluaran=1_000_000, masukan=400_000)
        w = spt.worksheet(self.store, 2026, 3)
        self.assertEqual((w["out"], w["in"], w["to_pay"], w["overpaid"]), (1_000_000, 400_000, 600_000, 0))

    def test_overpayment_offsets_next_month(self):
        self.post(datetime.date(2026, 3, 10), keluaran=100_000, masukan=500_000)
        self.post(datetime.date(2026, 4, 10), keluaran=300_000)
        march = spt.worksheet(self.store, 2026, 3)
        april = spt.worksheet(self.store, 2026, 4)
        self.assertEqual(march["overpaid"], 400_000)
        self.assertEqual((april["carry_in"], april["to_pay"], april["overpaid"]), (400_000, 0, 100_000))

    def test_page_and_csv(self):
        self.post(datetime.date(2026, 3, 10), keluaran=110_000)
        page = self.client.get(reverse("merchant-spt-ppn"), {"month": "2026-03"})
        self.assertContains(page, "SPT Masa PPN")
        csv = self.client.get(reverse("merchant-spt-ppn"), {"month": "2026-03", "format": "csv"})
        self.assertIn("PPN keluaran", csv.content.decode("utf-8"))
