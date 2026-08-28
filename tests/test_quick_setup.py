from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import ShippingMethod
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class QuickSetupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("setup-owner", password="test")
        self.store = Store.objects.create(owner=self.owner, name="Setup Shop", slug="setup-shop")
        self.client.force_login(self.owner)

    def test_custom_homepage_content_is_rendered(self):
        self.store.hero_eyebrow = "BUATAN LOKAL"
        self.store.hero_title = "Judul toko saya"
        self.store.hero_button_label = "Belanja sekarang"
        self.store.trust_title_1 = "Garansi resmi"
        self.store.save()
        response = self.client.get("/")
        self.assertContains(response, "BUATAN LOKAL")
        self.assertContains(response, "Judul toko saya")
        self.assertContains(response, "Belanja sekarang")
        self.assertContains(response, "Garansi resmi")

    def test_quick_defaults_are_idempotent(self):
        self.client.post("/merchant/quick-defaults/")
        self.client.post("/merchant/quick-defaults/")
        self.assertEqual(Warehouse.objects.filter(store=self.store, code="MAIN").count(), 1)
        self.assertEqual(ShippingMethod.objects.filter(store=self.store, code="reguler").count(), 1)

    def test_csv_import_creates_and_updates_product_and_stock(self):
        first = SimpleUploadedFile("products.csv",
            b"name,sku,price,stock,category,description,weight_grams,image_url\nKaos,KAOS-1,99000,8,Pakaian,Katun,250,\n",
            content_type="text/csv")
        response = self.client.post("/merchant/products/import/", {"file": first})
        self.assertRedirects(response, "/merchant/setup/products/")
        product = Product.objects.get(store=self.store, sku="KAOS-1")
        self.assertEqual(product.price, 99000)
        self.assertEqual(product.category.name, "Pakaian")
        self.assertEqual(InventoryLevel.objects.get(product=product).on_hand, 8)

        second = SimpleUploadedFile("products.csv",
            b"name,sku,price,stock\nKaos Baru,KAOS-1,109000,12\n", content_type="text/csv")
        self.client.post("/merchant/products/import/", {"file": second})
        product.refresh_from_db()
        self.assertEqual(Product.objects.filter(store=self.store, sku="KAOS-1").count(), 1)
        self.assertEqual(product.name, "Kaos Baru")
        self.assertEqual(product.price, 109000)
        self.assertEqual(InventoryLevel.objects.get(product=product).on_hand, 12)
