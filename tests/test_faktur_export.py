"""Faktur Pajak Keluaran worksheet: one row per line, whole-rupiah split, problems flagged."""
import io
from decimal import Decimal

from django.urls import reverse
from openpyxl import load_workbook
from test_sales_docs import Base

from apps.tax import faktur


class FakturTests(Base):
    def test_spread_adds_up_to_the_total(self):
        parts = faktur._spread(Decimal(1000), [Decimal(1), Decimal(1), Decimal(1)])
        self.assertEqual(sum(parts), Decimal(1000))

    def test_buyer_id_rules(self):
        self.assertEqual(faktur.buyer_problem(""), "NPWP/NIK pembeli kosong")
        self.assertIn("bukan 15 atau 16", faktur.buyer_problem("123"))
        self.assertIsNone(faktur.buyer_problem("0123456789012345"))

    def test_empty_month_downloads_with_the_limitation_note(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("merchant-faktur-export"), {"month": f"{self.today:%Y-%m}"})
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ["Catatan", "Faktur keluaran", "Nota retur"])
        text = " ".join(str(c.value) for row in wb["Catatan"].iter_rows() for c in row if c.value)
        self.assertIn("bukan file XML", text)

    def test_spt_page_links_to_it(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-spt-ppn"))
        self.assertContains(page, reverse("merchant-faktur-export"))

    def test_rows_split_the_invoice_tax_and_flag_a_missing_buyer_id(self):
        invoice = self.invoice(kabel=2, lampu=1)
        self.assertGreater(invoice.tax, 0)
        data = faktur.rows(self.store, invoice.issued_on.year, invoice.issued_on.month)
        mine = [r for r in data["invoices"] if r["number"] == invoice.number]
        self.assertEqual(len(mine), 2)
        self.assertEqual(sum(r["ppn"] for r in mine), invoice.tax)
        self.assertEqual(sum(r["dpp"] for r in mine), invoice.tax_base)
        self.assertIn((invoice.number, "NPWP/NIK pembeli kosong"), data["problems"])
