"""Excel reports, the auditor pack and the SAK EMKM pack."""
import datetime
import io
from decimal import Decimal

from django.urls import reverse
from openpyxl import load_workbook
from test_sales_docs import Base

from apps.accounting import closing, emkm
from apps.accounting.ledger import CASH, COGS, SALES, post_entry
from apps.shops.models import StoreStaff


def sheets(response):
    assert response.status_code == 200, response.status_code
    return load_workbook(io.BytesIO(response.content))


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.year = datetime.date.today().year - 1
        post_entry(self.store, date=datetime.date(self.year, 5, 1), memo="jual", amounts={CASH: 1000, SALES: -1000},
            source=("test", "a"))
        post_entry(self.store, date=datetime.date(self.year, 6, 1), memo="hpp", amounts={COGS: 300, CASH: -300},
            source=("test", "b"))
        self.client.force_login(self.owner)

    def test_each_report_downloads_as_a_real_workbook(self):
        start, end = f"{self.year}-01-01", f"{self.year}-12-31"
        for kind, title in [("laba-rugi", "Laba rugi"), ("neraca", "Neraca"), ("arus-kas", "Arus kas"),
                ("neraca-saldo", "Neraca saldo"), ("buku-besar", "Buku besar")]:
            wb = sheets(self.client.get(reverse("merchant-report-xlsx", args=[kind]),
                {"start": start, "end": end, "as_of": end}))
            self.assertEqual(wb.sheetnames, [title])

    def test_profit_loss_sheet_holds_numbers_that_match_the_screen(self):
        wb = sheets(self.client.get(reverse("merchant-report-xlsx", args=["laba-rugi"]),
            {"start": f"{self.year}-01-01", "end": f"{self.year}-12-31"}))
        values = {row[1]: row[2] for row in wb.active.iter_rows(values_only=True) if row[1]}
        self.assertEqual(values["Laba bersih"], 700)
        self.assertIsInstance(values["Laba bersih"], (int, float))

    def test_unknown_report_is_404(self):
        self.assertEqual(self.client.get(reverse("merchant-report-xlsx", args=["x"])).status_code, 404)

    def test_audit_pack_has_every_sheet_and_the_journal_lines(self):
        wb = sheets(self.client.get(reverse("merchant-audit-pack"), {"year": self.year}))
        self.assertEqual(wb.sheetnames, ["Ringkasan", "Neraca saldo", "Laba rugi", "Neraca", "Jurnal", "Piutang",
            "Utang", "Aset tetap", "Persediaan", "Penjualan", "PPN", "PPh", "Log aktivitas"])
        rows = list(wb["Jurnal"].iter_rows(min_row=5, values_only=True))
        self.assertEqual(sum(row[6] or 0 for row in rows), sum(row[7] or 0 for row in rows))
        self.assertEqual(len(rows), 4)

    def test_audit_pack_without_log_and_accountant_can_download(self):
        wb = sheets(self.client.get(reverse("merchant-audit-pack"), {"year": self.year, "log": "0"}))
        self.assertNotIn("Log aktivitas", wb.sheetnames)
        from django.contrib.auth.models import User
        auditor = User.objects.create_user("auditor", password="x")
        StoreStaff.objects.create(store=self.store, user=auditor, role="accountant")
        self.client.force_login(auditor)
        self.assertEqual(self.client.get(reverse("merchant-audit-pack"), {"year": self.year}).status_code, 200)

    def test_emkm_pack_matches_the_books_and_renders_pdf(self):
        pack = emkm.pack(self.store, self.year)
        self.assertEqual(pack["net_profit"], Decimal(700))
        self.assertTrue(pack["balanced"])
        page = self.client.get(reverse("merchant-emkm-pack"), {"year": self.year})
        for text in ["Laporan Posisi Keuangan", "Laporan Laba Rugi", "Catatan atas Laporan Keuangan"]:
            self.assertContains(page, text)
        pdf = self.client.get(reverse("merchant-emkm-pack"), {"year": self.year, "format": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_closed_year_still_exports_its_profit(self):
        closing.close_year(self.store, self.year, today=datetime.date.today())
        wb = sheets(self.client.get(reverse("merchant-report-xlsx", args=["laba-rugi"]),
            {"start": f"{self.year}-01-01", "end": f"{self.year}-12-31"}))
        values = {row[1]: row[2] for row in wb.active.iter_rows(values_only=True) if row[1]}
        self.assertEqual(values["Laba bersih"], 700)
