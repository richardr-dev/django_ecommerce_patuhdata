"""The marketplace-style homepage: category circles with icons, and products with several photos
uploaded at once."""
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.catalog.models import Category, Product

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89")


def _png(name):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class SimpleHomeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        from apps.shops.models import Store
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.client.force_login(self.owner)

    def test_categories_show_their_icon_or_first_product_photo_or_initial(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            Category.objects.create(store=self.store, name="Aksesoris", slug="aksesoris",
                image=_png("icon.png"))
            baju = Category.objects.create(store=self.store, name="Baju", slug="baju")
            Product.objects.create(store=self.store, category=baju, name="Kaos", slug="kaos",
                sku="KAOS", price=50000, image_url="https://cdn.example/kaos.jpg")
            Category.objects.create(store=self.store, name="Celana", slug="celana")
            home = self.client.get("/").content.decode()

        strip = home.split('class="category-strip"')[1].split("</nav>")[0]
        self.assertIn("/media/categories/", strip)
        self.assertIn("https://cdn.example/kaos.jpg", strip)
        self.assertIn('<span class="category-icon">C</span>', strip)
        self.assertIn('href="/?category=baju#catalog"', strip)

    def test_browsing_hides_banner_categories_and_flash_sale(self):
        Category.objects.create(store=self.store, name="Baju", slug="baju")
        self.assertNotContains(self.client.get("/?q=kaos"), "category-strip")

    def test_product_takes_several_photos_at_once(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post("/merchant/setup/products/new/", {"name": "Kaos",
                "slug": "kaos", "sku": "KAOS", "price": "50000", "is_active": "on",
                "weight_grams": 0, "length_cm": 0, "width_cm": 0, "height_cm": 0,
                "photos": [_png("depan.png"), _png("belakang.png"), _png("detail.png")]})
            self.assertEqual(response.status_code, 302,
                response.context and response.context["form"].errors)
            product = Product.objects.get(sku="KAOS")
            self.assertTrue(product.photo.name.startswith("products/"))
            self.assertEqual(product.images.count(), 2)

            edit = f"/merchant/setup/products/{product.id}/"
            self.client.post(edit, {"name": "Kaos", "slug": "kaos", "sku": "KAOS",
                "price": "50000", "is_active": "on", "weight_grams": 0, "length_cm": 0,
                "width_cm": 0, "height_cm": 0, "photos": [_png("baru.png")]})
            self.assertEqual(list(product.images.values_list("position", flat=True)), [1, 2, 3])
