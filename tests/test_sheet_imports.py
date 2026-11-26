"""Excel/CSV files into invoices, receipts and customers: our template, a hand-kept sales sheet, and Accurate's
own import files (the headers of ``sales-invoice-import-file``, ``sales-receipt-import-file`` and
``customer-import-id``)."""
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook
from test_sales_docs import Base

from apps.shops.models import Customer
from apps.tax import invoice_batch as batch
from apps.tax import receipt_import, sheet_import
from apps.tax.models import Invoice

D = Decimal


def xlsx(rows, name="f.xlsx"):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue())


SALES_HEAD = ["Date", "SKU", "Qty", "Customer Name", "Address", "Phone Number", "District", "Province",
              "Sales Channel", "Status", "Sales", "Net Sales", "Ongkir"]
ACC_HEAD = ["CUSTOMER NO", "NUMBER", "DATE", "DUE DATE", "ITEM:ITEM NO", "ITEM:QUANTITY", "ITEM:UNITPRICE",
            "ITEM:ITEM DISCOUNT (RP)", "EXPENSE:EXPENSE VALUE"]


class InvoiceSheetTests(Base):
    def test_a_hand_kept_sales_sheet_becomes_one_invoice_per_row(self):
        text, notes = sheet_import.invoice_rows(self.store, xlsx([SALES_HEAD,
            ["03/08/2026", "KBL", 2, "Budi Baru", "Jl. Pakan 29", "08123", "Hulu", "Kalsel", "Shopee Toko Mitra", "Sales",
             240000, 150000, ""],
            ["04/08/2026", "LMP", 1, "Marulak", "Jl. Cut Mutia", "082115", "Lampung", "Lampung", "Mitra", "Retur",
             50000, 30000, 5000],
            ["05/08/2026", "LMP", 1, "Sari", "", "", "", "", "Manual", "Sales", 50000, "", ""]]))
        self.assertEqual(len(text.splitlines()), 2)                 # the Retur row is left out
        self.assertTrue(any("Status selain Sales" in n for n in notes))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual([(p.buyer_name, p.channel, p.phone, p.ok) for p in plans],
            [("Budi Baru", "shopee", "08123", True), ("Sari", "manual", "", True)])
        self.assertEqual(plans[0].lines[0]["unit_price"], D(120000))   # Sales / Qty
        made = batch.create_all(self.store, plans)
        self.assertEqual([i.order.channel for i in made], ["shopee", "manual"])
        self.assertEqual(Customer.objects.get(name="Budi Baru").address, "Jl. Pakan 29, Hulu, Kalsel")

    def test_accurate_invoice_file_continues_rows_and_needs_known_customers(self):
        rows = [ACC_HEAD,
            ["C.00001", "SI.1", "21/04/2026", "", "KBL", 5, 20000, "", ""],
            ["", "", "", "", "LMP", 6, 60000, 1000, ""],
            ["", "", "", "", "", "", "", "", 1000000]]            # an expense row: no item
        with self.assertRaisesRegex(ValueError, "C.00001 belum ada"):
            sheet_import.invoice_rows(self.store, xlsx(rows))
        Customer.objects.create(store=self.store, name="PT Cipta", customer_code="C.00001")
        text, notes = sheet_import.invoice_rows(self.store, xlsx(rows))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual(len(plans), 1)
        self.assertEqual((plans[0].reference, plans[0].buyer_name, len(plans[0].lines)), ("SI.1", "PT Cipta", 2))
        self.assertEqual(plans[0].shipping, D(1000000))          # the expense row is the invoice's ongkir
        self.assertTrue(any("ongkir" in n for n in notes))
        made = batch.create_all(self.store, plans)[0]
        self.assertEqual(made.shipping, D(1000000))
        self.assertEqual(made.total, made.subtotal - made.discount + made.tax + D(1000000))

    def test_our_template_invoices_the_ongkir_and_a_sales_sheet_does_not(self):
        head = ["Date", "Ref", "SKU", "Qty", "Customer Name", "Status", "Sales", "Ongkir"]
        text, notes = sheet_import.invoice_rows(self.store, xlsx([head,
            ["03/08/2026", "A", "KBL", 1, "Budi", "Sales", 100000, 15000],
            ["03/08/2026", "A", "LMP", 1, "Budi", "Sales", 50000, ""]]))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual((len(plans), plans[0].shipping, len(plans[0].lines)), (1, D(15000), 2))
        self.assertFalse(any("Ongkir dan Net Sales" in n for n in notes))
        text, notes = sheet_import.invoice_rows(self.store, xlsx([SALES_HEAD,
            ["03/08/2026", "KBL", 1, "Budi", "", "", "", "", "Shopee", "Sales", 100000, 90000, 15000]]))
        self.assertEqual(batch.parse_paste(self.store, text)[0].shipping, 0)
        self.assertTrue(any("Ongkir dan Net Sales" in n for n in notes))

    def test_a_pasted_ongkir_that_is_not_a_number_is_named(self):
        plans = batch.parse_paste(self.store, "A;Budi;03/08/2026;;KBL;1;100000;;;;;abc")
        self.assertIn("ongkir “abc” tidak terbaca", plans[0].errors[0])

    def test_the_page_checks_an_upload_then_creates(self):
        self.client.force_login(self.owner)
        upload = xlsx([SALES_HEAD, ["03/08/2026", "KBL", 1, "Budi", "", "", "", "", "Manual", "Sales", 100000, "", ""]])
        page = self.client.post(reverse("merchant-invoice-batch"), {"action": "upload", "file": upload})
        self.assertContains(page, "Buat 1 invoice")
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertContains(self.client.get(reverse("merchant-invoice-batch-template")), "Customer Name")


class ReceiptSheetTests(Base):
    def test_accurate_receipt_file_settles_several_invoices_in_one_receipt(self):
        self.customer.customer_code = "C.00001"
        self.customer.save()
        one, two = self.invoice(), self.invoice(kabel=1)
        head = ["CUSTOMER NO", "NUMBER", "BRANCH", "DATE", "BANK ACCOUNT NO", "DESCRIPTION", "PAYMENT TOTAL",
                "PAYMENT NUMBER", "PAYMENT VALUE", "PAYING BANK", "DISCOUNT ACCOUNT NO", "TOTAL DISCOUNT"]
        text, notes = sheet_import.receipt_rows(xlsx([head,
            ["C.00001", "RCP-1", "Pusat", "01/06/2026", "110101", "Bayar", 0, one.number, 100000, "Transfer Bank", "", 5000],
            ["", "", "", "", "", "", None, two.number, 50000, "", "", ""]]))
        self.assertTrue(any("diskon" in n for n in notes))
        plans = receipt_import.plan(self.store, text, default_bank=self.main)
        self.assertEqual((len(plans), plans[0].ok, plans[0].total), (1, True, D(150000)))
        self.assertIn("tidak dikenal", plans[0].warnings[0])        # their account code is not ours: the chosen one
        made = receipt_import.create_all(self.store, plans)
        self.assertEqual(made[0].amount, D(150000))
        one.refresh_from_db()
        self.assertEqual(one.amount_paid, D(100000))

    def test_an_old_invoice_number_kept_as_reference_is_found(self):
        from apps.tax import invoice_batch as batch
        made = batch.create_all(self.store, batch.parse_paste(self.store, "SI.2026.001;Budi;;;KBL;1;;\n"))
        plans = receipt_import.plan(self.store, "\tBudi\tRCP-9\tsi.2026.001\t50000\t\t\t\n", default_bank=self.main)
        self.assertTrue(plans[0].ok, plans[0].errors)
        receipt_import.create_all(self.store, plans)
        made[0].refresh_from_db()
        self.assertEqual(made[0].amount_paid, D(50000))

    def test_mistakes_are_named_and_nothing_is_made(self):
        invoice = self.invoice()
        plans = receipt_import.plan(self.store, f"\tNo Such\t\t{invoice.number}\t100000\t\t\t\n"
            f"\tBudi\t\tINV-NOPE\t1000\t\t\t\n\tBudi\t\t{invoice.number}\t99999999\t\t\t\n")
        self.assertEqual([p.ok for p in plans], [False, False])
        self.assertIn("tidak ada di Pelanggan", plans[0].errors[0])
        self.assertTrue(any("tidak ditemukan" in e for e in plans[1].errors))
        self.assertTrue(any("sisa tagihan" in e for e in plans[1].errors))
        with self.assertRaises(receipt_import.receipts.ReceiptError):
            receipt_import.create_all(self.store, plans)

    def test_the_page_opens_and_the_template_downloads(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-receipt-import")), "Impor penerimaan")
        self.assertContains(self.client.get(reverse("merchant-receipt-import-template")), "No. invoice")


class AccurateCustomerTests(Base):
    def test_accurate_customer_template_is_read(self):
        from apps.shops import customer_import
        from apps.shops.tabular import read_rows
        _, rows = read_rows(xlsx([["Kategori", "ID Pelanggan", "Nama", "Handphone", "Email", "Alamat Penagihan", "Kota",
            "Nomor Wajib Pajak", "Syarat Pembayaran", "Jumlah Limit Piutang"],
            ["Member", "C.9", "Mr. Williams", "0812-111", "w@x.com", "Jl Camar", "Jakarta", "01.234.567.8-901.234", "net 30", 2000000],
            ["Umum", "", "Pelanggan Umum", "", "", "", "", "", "C.O.D", ""]]))
        created, _ = customer_import.import_customers(self.store, customer_import.parse(rows))
        self.assertEqual(created, 2)
        williams = Customer.objects.get(store=self.store, customer_code="C.9")
        self.assertEqual((williams.phone, williams.address, williams.payment_terms_days, int(williams.credit_limit)),
            ("0812111", "Jl Camar, Jakarta", 30, 2000000))
        self.assertEqual(williams.tags, "Member")
