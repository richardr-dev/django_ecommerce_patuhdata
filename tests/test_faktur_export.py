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


class FakturXmlTests(Base):
    """The Coretax XML follows the structure in faktur.xml_document; it is NOT import-tested against Coretax."""

    def setUp(self):
        super().setUp()
        from apps.tax.models import TaxProfile
        TaxProfile.objects.update_or_create(store=self.store, defaults={"npwp": "0123456789012345", "is_pkp": True})
        self.client.force_login(self.owner)

    def make(self, npwp="0987654321098765", unit="UM.0021"):
        from apps.tax.models import Invoice
        invoice = self.invoice(kabel=2, lampu=1)
        Invoice.objects.filter(pk=invoice.pk).update(buyer_npwp=npwp)
        invoice.items.update(unit=unit)
        return invoice

    def test_a_complete_invoice_becomes_the_documented_structure(self):
        from xml.etree import ElementTree as ET
        invoice = self.make()
        xml, skipped, _warnings = faktur.xml_document(self.store, invoice.issued_on.year, invoice.issued_on.month)
        self.assertEqual(skipped, [])
        root = ET.fromstring(xml)
        self.assertEqual((root.tag, root.find("TIN").text), ("TaxInvoiceBulk", "0123456789012345"))
        node = root.find("ListOfTaxInvoice/TaxInvoice")
        self.assertEqual([c.tag for c in node][:6], ["TaxInvoiceDate", "TaxInvoiceOpt", "TrxCode", "AddInfo", "CustomDoc", "CustomDocMonthYear"])
        self.assertEqual((node.find("SellerIDTKU").text, node.find("BuyerIDTKU").text),
            ("0123456789012345000000", "0987654321098765000000"))
        self.assertEqual((node.find("BuyerCountry").text, node.find("RefDesc").text), ("IDN", invoice.number))
        lines = node.findall("ListOfGoodService/GoodService")
        self.assertEqual(len(lines), 2)
        self.assertEqual([c.tag for c in lines[0]], ["Opt", "Code", "Name", "Unit", "Price", "Qty", "TotalDiscount",
            "TaxBase", "OtherTaxBase", "VATRate", "VAT", "STLGRate", "STLG"])
        self.assertEqual(sum(Decimal(line.find("TaxBase").text) for line in lines), invoice.tax_base)

    def test_eleven_percent_uses_dpp_nilai_lain(self):
        from xml.etree import ElementTree as ET
        invoice = self.make()
        node = ET.fromstring(faktur.xml_document(self.store, invoice.issued_on.year, invoice.issued_on.month)[0]
            ).find("ListOfTaxInvoice/TaxInvoice")
        if invoice.tax_rate == Decimal(11):
            self.assertEqual(node.find("TrxCode").text, "04")
            line = node.find("ListOfGoodService/GoodService")
            base, other = Decimal(line.find("TaxBase").text), Decimal(line.find("OtherTaxBase").text)
            self.assertEqual((line.find("VATRate").text, other), ("12", (base * 11 / 12).quantize(Decimal(1))))
            self.assertEqual(Decimal(line.find("VAT").text), (other * 12 / 100).quantize(Decimal(1)))

    def test_what_cannot_be_written_is_listed_not_dropped(self):
        invoice = self.make(npwp="", unit="")
        xml, skipped, _ = faktur.xml_document(self.store, invoice.issued_on.year, invoice.issued_on.month)
        self.assertIsNone(xml)
        self.assertIn("NPWP/NIK pembeli", skipped[0][1])
        self.assertIn("kode UM", skipped[0][1])

    def test_download_and_the_page_link(self):
        invoice = self.make()
        reply = self.client.get(reverse("merchant-faktur-xml"), {"month": f"{invoice.issued_on:%Y-%m}"})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply["Content-Disposition"].endswith('.xml"'))
        self.assertTrue(reply.content.startswith(b'<?xml version="1.0" encoding="utf-8" ?>'))
        self.assertContains(self.client.get(reverse("merchant-spt-ppn")), reverse("merchant-faktur-xml"))
