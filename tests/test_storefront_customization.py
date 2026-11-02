import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.orders.forms import StoreSettingsForm
from apps.shops.models import AuditLog, Store
from apps.shops.storefront_templates import STOREFRONT_TEMPLATES

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89")


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class StorefrontCustomizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("design-owner")
        self.store = Store.objects.create(owner=self.owner, name="Design Shop", slug="design-shop",
            hero_title="Judul milik merchant")
        self.client.force_login(self.owner)

    def _settings_data(self, **overrides):
        data = {field: getattr(self.store, field) for field in StoreSettingsForm.Meta.fields
            if field not in {"logo", "cover_image"}}
        data.update({"domain": "", "tax_rate": "0", **overrides})
        return {key: value for key, value in data.items() if value is not None}

    def test_applying_template_sets_design_but_keeps_merchant_content(self):
        response = self.client.post("/merchant/settings/template/", {"template": "boutique"})
        self.assertRedirects(response, "/merchant/settings/")
        self.store.refresh_from_db()
        for field, value in STOREFRONT_TEMPLATES["boutique"]["values"].items():
            self.assertEqual(getattr(self.store, field), value)
        self.assertEqual(self.store.storefront_template, "boutique")
        self.assertEqual(self.store.hero_title, "Judul milik merchant")
        self.assertTrue(AuditLog.objects.filter(store=self.store, action="store.template_applied").exists())
        settings_page = self.client.get("/merchant/settings/")
        self.assertContains(settings_page, "Sedang dipakai ✓")

    def test_unknown_template_is_rejected(self):
        self.client.post("/merchant/settings/template/", {"template": "nope"})
        self.store.refresh_from_db()
        self.assertEqual(self.store.storefront_template, "")

    def test_storefront_renders_design_tokens_logo_size_and_font(self):
        self.store.primary_color, self.store.background_color = "#2563eb", "#f8fafc"
        self.store.font_family, self.store.logo_size = "serif", "large"
        self.store.save()
        response = self.client.get("/")
        self.assertContains(response, "--brand:#2563eb")
        self.assertContains(response, "--page-bg:#f8fafc")
        self.assertContains(response, "--store-font:ui-serif")
        self.assertContains(response, 'class="font-serif logo-large"')

    def test_several_banners_upload_at_once_and_show_as_landscape_slider(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            banners = [SimpleUploadedFile(f"banner{n}.png", PNG, content_type="image/png")
                for n in range(3)]
            response = self.client.post("/merchant/settings/", self._settings_data(
                show_store_name="on", banners=banners))
            self.assertRedirects(response, "/merchant/settings/")
            slides = list(self.store.storefront_slides.all())
            self.assertEqual([slide.position for slide in slides], [1, 2, 3])
            home = self.client.get("/")
            self.assertContains(home, "commercial-carousel")
            self.assertContains(home, 'class="carousel-slide"', count=3)

            self.client.post("/merchant/settings/", self._settings_data(show_store_name="on",
                remove_banners=[slides[0].id]))
            self.assertEqual(self.store.storefront_slides.count(), 2)

    def test_without_a_banner_the_top_of_the_store_shows_nothing(self):
        home = self.client.get("/")
        self.assertNotContains(home, "commercial-carousel")
        self.assertNotContains(home, "home-hero")
        self.assertNotContains(home, "Judul milik merchant")

    def test_an_existing_cover_photo_still_shows_as_the_banner(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            self.store.cover_image = SimpleUploadedFile("cover.png", PNG, content_type="image/png")
            self.store.save()
            home = self.client.get("/")
        self.assertContains(home, "single-banner")
        self.assertContains(home, self.store.cover_image.url)

    def test_banner_uploads_must_be_images(self):
        form = StoreSettingsForm(data=self._settings_data(), files={"banners": [
            SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")]},
            instance=self.store, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("banners", form.errors)

    def test_store_name_can_be_hidden_next_to_logo(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            self.store.logo = SimpleUploadedFile("logo.png", PNG, content_type="image/png")
            self.store.show_store_name = False
            self.store.save()
            response = self.client.get("/")
        header = response.content.decode().split("</header>")[0]
        self.assertIn("store-logo", header)
        self.assertNotIn("<span>Design Shop</span>", header)

    def test_colors_must_be_hex_to_be_safe_inside_style_tag(self):
        form = StoreSettingsForm(data=self._settings_data(
            primary_color="red;}body{display:none", background_color="#fff"),
            instance=self.store, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("primary_color", form.errors)
        self.assertIn("background_color", form.errors)

    def test_theme_picker_previews_every_preset_including_asian_market_looks(self):
        page = self.client.get("/merchant/settings/")
        self.assertContains(page, 'class="theme-preview"', count=len(STOREFRONT_TEMPLATES))
        self.assertContains(page, "Promo Merah")
        self.assertContains(page, "Pastel Manis")
