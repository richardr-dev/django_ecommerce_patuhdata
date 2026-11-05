"""Pelacakan & pixel: validated IDs only, loaded on the storefront behind cookie consent."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.orders.models import Order
from apps.shops.models import Store


@override_settings(PLATFORM_DOMAIN="")
class TrackingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _save(self, **data):
        fields = {"ga4_id": "", "gtm_id": "", "google_ads_id": "", "meta_pixel_id": "",
            "tiktok_pixel_id": "", "clarity_id": "", "google_site_verification": ""}
        return self.seller.post("/merchant/tracking/", {**fields, **data})

    def test_ids_are_saved_and_rendered_behind_consent(self):
        self._save(ga4_id="g-ab12cd34ef", meta_pixel_id="1234567890123456",
            google_site_verification='<meta name="google-site-verification" '
                'content="abcDEF1234567890_xyz-QQ-padded-to-be-long-like-the-real-ones-are-ok" />')
        self.store.refresh_from_db()
        self.assertEqual(self.store.ga4_id, "G-AB12CD34EF")
        self.assertEqual(self.store.google_site_verification, "abcDEF1234567890_xyz-QQ-padded-to-be-long-like-the-real-ones-are-ok")
        page = Client().get("/").content.decode()
        self.assertIn('id="store-tracking"', page)
        self.assertIn('"G-AB12CD34EF"', page)
        self.assertIn('content="abcDEF1234567890_xyz-QQ-padded', page)
        self.assertIn("storefront-tracking.js", page)
        self.assertNotIn("googletagmanager.com/gtag/js", page)  # loaded by script after consent

    def test_scripts_cannot_be_pasted(self):
        reply = self._save(ga4_id="G-1</script><script>alert(1)</script>", meta_pixel_id="abc")
        self.assertContains(reply, "Format tidak dikenali")
        self.assertContains(reply, "errorlist", count=2)
        self.store.refresh_from_db()
        self.assertEqual(self.store.ga4_id, "")
        self.assertNotIn("store-tracking", Client().get("/").content.decode())

    def test_paid_order_page_reports_the_purchase(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=100000, total=100000, status="paid",
            idempotency_key="t1")
        page = Client().get(f"/orders/{order.id}/?token={order.access_token}")
        self.assertContains(page, 'data-track-purchase')
        self.assertContains(page, 'data-value="100000"')
