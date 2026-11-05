"""Digital products: no address or courier at checkout, no stock, and the private file is
downloadable only from a paid order."""
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings

from apps.catalog.models import Product
from apps.orders.models import Order
from apps.shops.models import Store

MEDIA = Path(tempfile.mkdtemp())


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, PLATFORM_DOMAIN="",
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="", MEDIA_ROOT=MEDIA)
class DigitalProductTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        from django.utils.functional import empty

        from apps.catalog.private_files import private_storage
        private_storage._wrapped = empty  # re-create it for this MEDIA_ROOT
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.ebook = Product.objects.create(store=self.store, name="E-book Kopi", slug="ebook",
            sku="EBOOK", price=45000, is_digital=True)
        self.ebook.digital_file.save("panduan.pdf", ContentFile(b"%PDF-1.4 kopi"), save=True)

    def test_file_is_stored_outside_the_public_media_folder(self):
        self.assertTrue(self.ebook.digital_file.name.startswith("digital/ruuma/"))
        self.assertTrue((MEDIA / "private" / self.ebook.digital_file.name).exists())
        self.assertFalse((MEDIA / self.ebook.digital_file.name).exists())

    def test_digital_checkout_needs_no_address_and_downloads_after_payment(self):
        shopper = Client()
        shopper.post(f"/cart/add/{self.ebook.id}/")
        page = shopper.get("/checkout/")
        self.assertContains(page, "Produk digital: tanpa pengiriman")
        self.assertNotContains(page, 'name="shipping_address"')
        shopper.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_method": "digital"})
        order = Order.objects.get()
        self.assertEqual((order.shipping_cost, order.shipping_address), (0, "Produk digital"))
        item = order.items.get()
        url = f"/orders/{order.id}/download/{item.id}/?token={order.access_token}"
        Order.objects.filter(pk=order.pk).update(status="pending")
        self.assertEqual(Client().get(url).status_code, 404)  # not paid yet
        Order.objects.filter(pk=order.pk).update(status="paid")
        download = Client().get(url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 kopi")
        self.assertIn("panduan.pdf", download["Content-Disposition"])
        self.assertEqual(Client().get(url.split("?")[0]).status_code, 404)  # no token
        self.assertContains(Client().get(f"/orders/{order.id}/?token={order.access_token}"), "Siap diunduh")

    def test_a_digital_product_needs_a_file_or_link(self):
        seller = Client()
        seller.force_login(self.owner)
        reply = seller.post("/merchant/setup/products/new/", {"name": "Kelas", "slug": "kelas",
            "sku": "KELAS", "price": "99000", "is_active": "on", "is_digital": "on",
            "weight_grams": 0, "length_cm": 0, "width_cm": 0, "height_cm": 0, "preorder_days": 1})
        self.assertContains(reply, "Unggah file atau isi link digital")
