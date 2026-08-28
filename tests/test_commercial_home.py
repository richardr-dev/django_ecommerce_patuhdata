from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.catalog.models import Product
from apps.orders.forms import StoreSettingsForm
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class CommercialHomepageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("commercial-owner")
        self.store = Store.objects.create(owner=self.owner, name="Commercial Shop", slug="commercial-shop",
            announcement_text="Gratis ongkir akhir pekan", announcement_link_label="Belanja sekarang",
            announcement_link_url="/#catalog", campaign_eyebrow="KHUSUS HARI INI",
            campaign_title="Lengkapi koleksi Anda", campaign_text="Stok promosi terbatas.",
            campaign_button_label="Ambil penawaran", campaign_link_url="/#catalog")
        self.featured = Product.objects.create(store=self.store, name="Produk Unggulan",
            slug="produk-unggulan", sku="FEATURED-1", price=150000, is_featured=True)
        Product.objects.create(store=self.store, name="Produk Reguler", slug="produk-reguler",
            sku="REGULAR-1", price=100000)

    def test_commercial_sections_render_from_store_settings(self):
        response = self.client.get("/")
        self.assertContains(response, "Gratis ongkir akhir pekan")
        self.assertContains(response, "Produk unggulan")
        self.assertContains(response, "Produk Unggulan")
        self.assertContains(response, "Lengkapi koleksi Anda")
        self.assertContains(response, "Ambil penawaran")

    def test_campaign_links_reject_unsafe_schemes(self):
        data = {field: getattr(self.store, field) for field in StoreSettingsForm.Meta.fields}
        data["name"] = self.store.name
        data["theme"] = "modern"
        data["primary_color"] = "#155eef"
        data["currency"] = "IDR"
        data["tax_rate"] = "0"
        data["campaign_link_url"] = "javascript:alert(1)"
        form = StoreSettingsForm(data=data, instance=self.store, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("campaign_link_url", form.errors)
