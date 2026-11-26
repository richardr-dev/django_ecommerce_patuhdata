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
            ["04/08/2026", "LMP", 1, "Joko", "", "", "", "", "Manual", "Batal", 50000, "", ""],
            ["05/08/2026", "LMP", 1, "Sari", "", "", "", "", "Manual", "Sales", 50000, "", ""]]))
        self.assertEqual(len(text.splitlines()), 3)                 # the Retur row is invoiced and credited
        self.assertTrue(any("Retur" in n for n in notes) and any("selain Sales" in n for n in notes))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual([(p.buyer_name, p.channel, p.phone, p.ok, bool(p.returned)) for p in plans],
            [("Budi Baru", "shopee", "08123", True, False), ("Marulak", "manual", "082115", True, True),
             ("Sari", "manual", "", True, False)])
        self.assertEqual(plans[0].lines[0]["unit_price"], D(120000))   # Sales / Qty
        made = batch.create_all(self.store, plans)
        self.assertEqual([i.order.channel for i in made], ["shopee", "manual", "manual"])
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

    def test_the_owners_sales_sheet_keeps_net_sales_ongkir_and_the_mitra(self):
        """The sheet from the photo: Sales at retail, Net Sales after the mitra's cut, Ongkir paid to the courier."""
        from apps.accounting.models import Expense, JournalLine
        from apps.settlements.models import ConsignmentPartner
        abshop = ConsignmentPartner.objects.create(store=self.store, name="ABShop")
        text, notes = sheet_import.invoice_rows(self.store, xlsx([SALES_HEAD,
            ["03/08/2026", "KBL", 1, "Budi", "Jalan Pakan Dalam no.29, Kec. Daha Utara, Kab. Hulu Sungai Selatan",
             "", "Hulu Sungai Selatan", "Kalimantan Selatan", "Shopee ABShop", "Sales", 1198000, 718800, ""],
            ["04/08/2026", "LMP", 1, "Marulak Purba", "Jl. Cut Mutia No. 42", "082115108880", "Bandar Lampung",
             "Lampung", "ABShop", "Sales", 998000, 598800, 26996],
            ["05/08/2026", "KBL", 1, "H. Aidil Azwar", "JL. Palem Raya no. 9", "", "Bekasi", "Jawa Barat",
             "Tokopedia", "Sales", "1,198,000", "", ""]]))
        self.assertTrue(any("Net Sales" in n for n in notes) and any("6700" in n for n in notes))
        plans = batch.parse_paste(self.store, text)
        self.assertTrue(all(p.ok for p in plans), [p.errors for p in plans])
        budi, marulak, aidil = plans
        self.assertEqual((budi.lines[0]["unit_price"], budi.lines[0]["discount"]), (D(1198000), D(479200)))
        self.assertEqual(budi.subtotal, D(718800))                        # the invoice is worth Net Sales
        self.assertEqual((budi.partner, marulak.partner, aidil.partner), (abshop, abshop, None))
        self.assertEqual((budi.address, aidil.channel), (
            "Jalan Pakan Dalam no.29, Kec. Daha Utara, Kab. Hulu Sungai Selatan, Kalimantan Selatan", "tokopedia"))
        self.assertEqual((marulak.shipping, marulak.shipping_cost), (0, D(26996)))
        self.assertEqual(aidil.lines[0]["discount"], 0)                   # no Net Sales: no discount
        made = batch.create_all(self.store, plans, cost_account=self.main)
        self.assertEqual(made[0].order.consignment_partner, abshop)
        self.assertIn("Shopee ABShop", made[0].notes)
        self.assertEqual(made[1].total, D(598800) + D("65868"))          # Net Sales + PPN; ongkir not billed
        cost = Expense.objects.get(store=self.store)
        self.assertEqual((cost.amount, cost.category, cost.bank_account), (D(26996), "6700", self.main))
        self.assertTrue(JournalLine.objects.filter(entry__expense=cost, account__code="6700",
            debit=D(26996)).exists())

    def test_no_pesanan_groups_rows_into_one_invoice_and_is_its_reference(self):
        head = ["Date", "No. Pesanan", "SKU", "Qty", "Customer Name", "Status", "Sales", "Net Sales", "Ongkir"]
        text, _notes = sheet_import.invoice_rows(self.store, xlsx([head,
            ["05/08/2026", "SHP-2", "KBL", 1, "Aidil", "Sales", 100000, 60000, 21000],
            ["05/08/2026", "SHP-2", "LMP", 1, "Aidil", "Sales", 50000, 30000, ""]]))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual((len(plans), plans[0].reference, len(plans[0].lines), plans[0].subtotal,
            plans[0].shipping_cost), (1, "SHP-2", 2, D(90000), D(21000)))

    def test_the_sample_file_gives_four_invoices_and_a_credit_note_for_the_retur(self):
        """contoh-penjualan-abshop.xlsx: 5 rows, two share an order number, one is Retur."""
        from apps.accounting.models import Expense
        from apps.tax.models import CreditNote
        head = ["Date", "No. Pesanan", "SKU", "Qty", "Customer Name", "Status", "Sales", "Net Sales", "Ongkir"]
        text, notes = sheet_import.invoice_rows(self.store, xlsx([head,
            ["03/08/2026", "S-1", "KBL", 1, "Budi", "Sales", 100000, 60000, 18500],
            ["04/08/2026", "S-2", "LMP", 1, "Marulak", "Sales", 50000, 30000, 26996],
            ["05/08/2026", "S-3", "KBL", 1, "Aidil", "Sales", 100000, 60000, 21000],
            ["05/08/2026", "S-3", "LMP", 1, "Aidil", "Sales", 50000, 30000, ""],
            ["06/08/2026", "S-4", "KBL", 1, "Sari", "Retur", 100000, 60000, 19000]]))
        self.assertTrue(any("Retur" in n for n in notes))
        plans = batch.parse_paste(self.store, text)
        self.assertEqual([len(p.lines) for p in plans], [1, 1, 2, 1])
        self.assertEqual((plans[3].returned, plans[3].returned_total), ([0], D(66600)))   # 60.000 + PPN
        before = self.on_hand(self.kabel)
        made = batch.create_all(self.store, plans)
        self.assertEqual(len(made), 4)
        note = CreditNote.objects.get(invoice=made[3])
        self.assertEqual((note.total, note.issued_on, note.reason), (D(66600), made[3].issued_on, "return"))
        made[3].refresh_from_db()
        self.assertEqual(made[3].amount_due, 0)
        self.assertEqual(self.on_hand(self.kabel), before - 2)        # Budi and Aidil keep theirs; Sari's came back
        self.assertEqual(sorted(int(e.amount) for e in Expense.objects.all()), [18500, 19000, 21000, 26996])

    def test_the_template_downloads_as_a_workbook_with_help(self):
        from openpyxl import load_workbook
        self.client.force_login(self.owner)
        response = self.client.get(reverse("merchant-invoice-batch-template"))
        book = load_workbook(io.BytesIO(response.content))
        self.assertEqual([c.value for c in book["Penjualan"][1]][:13], SALES_HEAD)
        self.assertIn("Petunjuk", book.sheetnames)
        text, _notes = sheet_import.invoice_rows(self.store, SimpleUploadedFile("t.xlsx", response.content))
        self.assertEqual(len(text.splitlines()), 3)

    def test_a_pasted_ongkir_that_is_not_a_number_is_named(self):
        plans = batch.parse_paste(self.store, "A;Budi;03/08/2026;;KBL;1;100000;;;;;;abc")
        self.assertIn("ongkir dibayar “abc” tidak terbaca", plans[0].errors[0])

    def test_the_page_checks_an_upload_then_creates(self):
        self.client.force_login(self.owner)
        upload = xlsx([SALES_HEAD, ["03/08/2026", "KBL", 1, "Budi", "", "", "", "", "Manual", "Sales", 100000, "", ""]])
        page = self.client.post(reverse("merchant-invoice-batch"), {"action": "upload", "file": upload})
        self.assertContains(page, "Buat 1 invoice")
        self.assertEqual(Invoice.objects.count(), 0)


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
