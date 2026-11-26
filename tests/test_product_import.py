"""Impor produk: SKU or product name as the main key, blank cells keep what the product has, and a preview
that writes nothing."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.setup_views import import_products, parse_import_rows, plan_import
from apps.shops.models import Store

D = Decimal


def csv_file(text):
    return SimpleUploadedFile("p.csv", text.encode())


class ProductImportKeyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pi-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-pi")
        self.main = Warehouse.objects.create(store=self.store, code="MAIN", name="Gudang Utama")
        self.kemeja = Product.objects.create(store=self.store, name="Kemeja Putih", slug="kemeja", sku="KMJ-1",
            price=99000, cost_price=D(60000), description="Katun")
        InventoryLevel.objects.create(warehouse=self.main, product=self.kemeja, on_hand=10)
        self.tanpa_sku = Product.objects.create(store=self.store, name="Celana Hitam", slug="celana", sku="",
            price=149000)
        self.client = Client()
        self.client.force_login(self.owner)

    def stock(self, product):
        return InventoryLevel.objects.get(product=product, warehouse=self.main).on_hand

    def test_sku_key_updates_only_the_cells_that_are_filled(self):
        rows = [{"sku": "KMJ-1", "price": "105000"}, {"sku": "BRU-1", "name": "Baru", "price": "5000"}]
        created, updated = import_products(self.store, parse_import_rows(rows), key="sku")
        self.assertEqual((created, updated), (1, 1))
        self.kemeja.refresh_from_db()
        self.assertEqual((self.kemeja.name, self.kemeja.price, self.kemeja.cost_price, self.kemeja.description),
            ("Kemeja Putih", D(105000), D(60000), "Katun"))
        self.assertEqual(self.stock(self.kemeja), 10)          # blank stock keeps the shelf
        self.assertEqual(self.stock(Product.objects.get(sku="BRU-1")), 0)

    def test_name_key_gives_a_product_made_by_hand_its_sku(self):
        rows = [{"name": "celana hitam", "sku": "CLN-2", "stock": "4"}]
        self.assertEqual(import_products(self.store, parse_import_rows(rows, key="name"), key="name"), (0, 1))
        self.tanpa_sku.refresh_from_db()
        self.assertEqual((self.tanpa_sku.sku, self.tanpa_sku.price), ("CLN-2", D(149000)))
        self.assertEqual(self.stock(self.tanpa_sku), 4)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2)

    def test_name_key_refuses_a_sku_that_belongs_to_another_product_and_writes_nothing(self):
        rows = [{"name": "Celana Hitam", "sku": "KMJ-1"}]
        plan = plan_import(self.store, parse_import_rows(rows, key="name"), key="name")
        self.assertEqual(plan[0].action, "error")
        with self.assertRaisesRegex(ValueError, "Baris 2: SKU KMJ-1 sudah dipakai produk Kemeja Putih"):
            import_products(self.store, parse_import_rows(rows, key="name"), key="name")
        self.tanpa_sku.refresh_from_db()
        self.assertEqual(self.tanpa_sku.sku, "")

    def test_a_new_product_needs_name_sku_and_price(self):
        with self.assertRaisesRegex(ValueError, "produk baru perlu name, sku, dan price"):
            import_products(self.store, parse_import_rows([{"sku": "X-9", "name": "Tanpa harga"}]))
        with self.assertRaisesRegex(ValueError, "Baris 2: sku wajib diisi"):
            parse_import_rows([{"name": "Tanpa SKU", "price": "1"}], key="sku")

    def test_two_products_with_the_same_name_ask_for_the_sku_key(self):
        Product.objects.create(store=self.store, name="Kemeja Putih", slug="kemeja-2", sku="KMJ-2", price=1)
        plan = plan_import(self.store, parse_import_rows([{"name": "Kemeja Putih", "price": "5"}], key="name"),
            key="name")
        self.assertIn("pakai kunci SKU", plan[0].error)

    def test_a_variant_sku_is_refused(self):
        ProductVariant.objects.create(product=self.kemeja, name="L", sku="KMJ-1-L", price=99000)
        with self.assertRaisesRegex(ValueError, "dipakai varian"):
            import_products(self.store, parse_import_rows([{"sku": "KMJ-1-L", "price": "1"}]))

    def test_the_page_previews_without_writing_then_imports_with_indonesian_headers(self):
        text = "kode barang;nama;harga jual;stok;harga pokok\nKMJ-1;;110000;12;\nSPT-9;Sepatu;250000;;150000\n"
        url = reverse("merchant-product-import")
        page = self.client.post(url, {"file": csv_file(text), "key": ["name", "sku"], "action": "preview"})
        self.assertEqual((page.context["summary"]["create"], page.context["summary"]["update"]), (1, 1))
        self.assertContains(page, "harga, stok")
        self.assertFalse(Product.objects.filter(sku="SPT-9").exists())
        self.client.post(url, {"file": csv_file(text), "key": ["name", "sku"], "action": "import"})
        self.kemeja.refresh_from_db()
        self.assertEqual((self.kemeja.price, self.stock(self.kemeja)), (D(110000), 12))
        self.assertEqual(Product.objects.get(sku="SPT-9").cost_price, D(150000))

    def test_the_switch_off_matches_by_name(self):
        url = reverse("merchant-product-import")
        page = self.client.post(url, {"file": csv_file("name,sku\nCelana Hitam,CLN-2\n"), "key": "name",
            "action": "preview"})
        self.assertEqual(page.context["plans"][0].action, "update")
        self.assertContains(page, "Nama produk")
        page = self.client.post(url, {"file": csv_file("name,price\nCelana Hitam,1\n"), "key": "sku",
            "action": "preview"})
        self.assertContains(page, "Kolom sku tidak ditemukan")
