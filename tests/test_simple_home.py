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

        chips = home.split('class="category-chips"')[1].split("</nav>")[0]
        self.assertIn("/media/stores/toko/categories/", chips)
        self.assertIn("https://cdn.example/kaos.jpg", chips)
        self.assertIn(">Celana</a>", chips)  # no icon: the name alone
        self.assertIn('href="/?category=baju#catalog"', chips)

    def test_browsing_hides_banner_categories_and_flash_sale(self):
        Category.objects.create(store=self.store, name="Baju", slug="baju")
        page = self.client.get("/?q=kaos")
        self.assertNotContains(page, "store-profile")
        self.assertContains(page, "profile-back")

    def test_product_takes_several_photos_at_once(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post("/merchant/setup/products/new/", {"name": "Kaos",
                "slug": "kaos", "sku": "KAOS", "price": "50000", "is_active": "on",
                "weight_grams": 0, "length_cm": 0, "width_cm": 0, "height_cm": 0,
                "photos": [_png("depan.png"), _png("belakang.png"), _png("detail.png")]})
            self.assertEqual(response.status_code, 302,
                response.context and response.context["form"].errors)
            product = Product.objects.get(sku="KAOS")
            self.assertTrue(product.photo.name.startswith("stores/toko/products/kaos/"))
            self.assertTrue(product.images.first().file.name.startswith("stores/toko/products/kaos/"))
            self.assertEqual(product.images.count(), 2)

            edit = f"/merchant/setup/products/{product.id}/"
            self.client.post(edit, {"name": "Kaos", "slug": "kaos", "sku": "KAOS",
                "price": "50000", "is_active": "on", "weight_grams": 0, "length_cm": 0,
                "width_cm": 0, "height_cm": 0, "photos": [_png("baru.png")]})
            self.assertEqual(list(product.images.values_list("position", flat=True)), [1, 2, 3])


class UploadFolderTests(TestCase):
    def test_every_image_kind_lands_in_its_store_folder_with_a_random_name(self):
        from apps.catalog.models import ProductImage
        from apps.shops.models import Store, StorefrontSlide
        from apps.shops.uploads import StoreUploadPath

        store = Store.objects.create(owner=User.objects.create_user("o"), name="Kopi Kita",
            slug="kopi-kita")
        product = Product(store=store, sku="KOPI 250/G", slug="k", name="K", price=1)
        cases = [
            (StoreUploadPath("branding"), store, "stores/kopi-kita/branding/"),
            (StoreUploadPath("banners"), StorefrontSlide(store=store), "stores/kopi-kita/banners/"),
            (StoreUploadPath("products"), product, "stores/kopi-kita/products/kopi-250g/"),
            (StoreUploadPath("products"), ProductImage(product=product),
                "stores/kopi-kita/products/kopi-250g/"),
        ]
        for upload_to, instance, folder in cases:
            path = upload_to(instance, "Foto KTP Budi.JPG")
            self.assertTrue(path.startswith(folder), path)
            self.assertTrue(path.endswith(".jpg"))
            self.assertNotIn("Budi", path)
        self.assertNotEqual(StoreUploadPath("banners")(store, "a.png"),
            StoreUploadPath("banners")(store, "a.png"))


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class ProfileHomeAndQuickAddTests(TestCase):
    def setUp(self):
        from apps.catalog.models import InventoryLevel, ProductVariant, Warehouse
        from apps.shops.models import Store

        self.store = Store.objects.create(owner=User.objects.create_user("o"), name="Ruuma",
            slug="ruuma", description="Roti & eclair", instagram="ruuma.id",
            support_phone="081234567890")
        warehouse = Warehouse.objects.create(store=self.store, name="Dapur", code="D1")
        self.eclair = Product.objects.create(store=self.store, name="Eclair", slug="eclair",
            sku="ECL", price=25000)
        InventoryLevel.objects.create(warehouse=warehouse, product=self.eclair, on_hand=5)
        self.box = Product.objects.create(store=self.store, name="Box", slug="box", sku="BOX",
            price=90000)
        ProductVariant.objects.create(product=self.box, name="Isi 6", sku="BOX-6", stock=3)

    def test_home_shows_the_store_profile_instead_of_the_site_header(self):
        page = self.client.get("/").content.decode()
        self.assertNotIn('class="site-header"', page)
        self.assertIn('class="profile-identity', page)
        self.assertIn("<h1>Ruuma</h1>", page)
        self.assertIn("instagram.com/ruuma.id", page)
        self.assertIn("wa.me/6281234567890", page)
        self.assertIn('data-focus-search', page)

    def test_simple_products_get_a_tap_to_add_button_and_variants_a_pick_link(self):
        page = self.client.get("/").content.decode()
        self.assertIn(f'action="/cart/add/{self.eclair.id}/"', page)
        self.assertIn("Pilih varian", page)
        self.assertNotIn(f'action="/cart/add/{self.box.id}/"', page)

    def test_quick_add_answers_with_the_cart_total_and_the_bar_shows_it(self):
        reply = self.client.post(f"/cart/add/{self.eclair.id}/", {"quantity": 2},
            HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(reply.json()["count"], 2)
        self.assertEqual(reply.json()["total"], 50000)

        page = self.client.get("/")
        self.assertContains(page, 'data-cart-total>Rp50.000')
        self.assertNotContains(page, "data-cart-bar hidden")

    def test_quick_add_errors_are_reported_without_a_redirect(self):
        reply = self.client.post(f"/cart/add/{self.box.id}/", HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(reply.status_code, 400)
        self.assertIn("Pilih varian", reply.json()["message"])
