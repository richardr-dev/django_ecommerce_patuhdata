import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.shops.models import Store, StorefrontSlide


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class StorefrontCarouselTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("carousel-owner")
        self.store = Store.objects.create(owner=self.owner, name="Carousel Shop", slug="carousel-shop")
        self.client.force_login(self.owner)

    def test_merchant_uploads_slide_and_storefront_renders_carousel(self):
        image = SimpleUploadedFile("campaign.webp", b"fake-webp", content_type="image/webp")
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post("/merchant/setup/slides/new/", {
                "image": image, "eyebrow": "PROMO TERBATAS", "headline": "Belanja lebih hemat",
                "subheadline": "Penawaran minggu ini", "button_label": "Lihat promo",
                "link_url": "/#catalog", "text_alignment": "left", "position": 1,
                "is_active": "on",
            })
            self.assertRedirects(response, "/merchant/setup/slides/")
            slide = StorefrontSlide.objects.get(store=self.store)
            storefront = self.client.get("/")
            self.assertContains(storefront, "commercial-carousel")
            self.assertContains(storefront, "Belanja lebih hemat")
            self.assertContains(storefront, slide.image.url)

    def test_only_active_current_store_slides_are_shown(self):
        other = Store.objects.create(owner=self.owner, name="Other", slug="carousel-other")
        StorefrontSlide.objects.create(store=self.store, image="active.webp", headline="Active campaign")
        StorefrontSlide.objects.create(store=self.store, image="inactive.webp", headline="Hidden campaign", is_active=False)
        StorefrontSlide.objects.create(store=other, image="other.webp", headline="Other campaign")
        response = self.client.get("/")
        self.assertContains(response, "Active campaign")
        self.assertNotContains(response, "Hidden campaign")
        self.assertNotContains(response, "Other campaign")
