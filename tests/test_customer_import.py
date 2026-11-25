import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product
from apps.shops.models import Customer, Store

HEAD = "nomor_pelanggan,nama,perusahaan,telepon,email,alamat,npwp,termin_hari,batas_kredit,tag\n"


class CustomerImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("imp", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-imp")
        self.client.force_login(self.owner)

    def post(self, text, name="p.csv", url="merchant-customer-import"):
        return self.client.post(reverse(url), {"file": SimpleUploadedFile(name, text.encode())}, follow=True)

    def test_template_downloads_and_page_opens(self):
        self.assertContains(self.client.get(reverse("merchant-customer-import")), "nomor_pelanggan")
        page = self.client.get(reverse("merchant-customer-import-template"))
        self.assertIn("nomor_pelanggan,nama", page.content.decode())

    def test_import_creates_numbers_and_updates_without_doubling(self):
        self.post(HEAD + "C-1,Budi,,0812-345,b@x.com,Bandung,,30,5.000.000,vip\n,Sari,,,,Jakarta,,,,\n")
        budi = Customer.objects.get(store=self.store, customer_code="C-1")
        self.assertEqual((budi.phone, budi.payment_terms_days, int(budi.credit_limit)), ("0812345", 30, 5000000))
        self.assertEqual(Customer.objects.get(name="Sari").customer_code, "PLG-00001")
        self.post(HEAD + "C-1,Budi Baru,,,,,,,,\n")
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 2)
        budi.refresh_from_db()
        self.assertEqual((budi.name, budi.address), ("Budi Baru", "Bandung"))   # empty cell keeps the address

    def test_one_bad_row_saves_nothing(self):
        response = self.post(HEAD + "C-1,Budi,,,,,,,,\nC-2,,,,,,,,,\n")
        self.assertContains(response, "Baris 3")
        self.assertFalse(Customer.objects.filter(store=self.store).exists())

    def test_xlsx_is_read(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(["nomor_pelanggan", "nama"])
        book.active.append(["X-9", "Dewi"])
        buffer = io.BytesIO()
        book.save(buffer)
        self.client.post(reverse("merchant-customer-import"),
            {"file": SimpleUploadedFile("c.xlsx", buffer.getvalue())})
        self.assertTrue(Customer.objects.filter(store=self.store, customer_code="X-9").exists())

    def test_product_import_stock_is_optional(self):
        self.post("name,sku,price\nKemeja,KMJ,99000.0\n", url="merchant-product-import")
        self.assertEqual(int(Product.objects.get(store=self.store, sku="KMJ").price), 99000)
