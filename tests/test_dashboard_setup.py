"""The simplified seller dashboard: setup checklist ticked from real data, and what needs doing."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.catalog.models import Product
from apps.integrations.models import StorePaymentSettings
from apps.orders.models import Order, ShippingMethod
from apps.orders.onboarding import setup_steps
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="doku", DOKU_CLIENT_ID="", DOKU_SECRET_KEY="",
    PLATFORM_COLLECTS_PAYMENTS=False)
class DashboardSetupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko Baru", slug="toko-baru")
        self.client.force_login(self.owner)

    def _done(self):
        return [step.done for step in setup_steps(self.store)]

    def test_new_store_starts_with_nothing_ticked(self):
        self.assertEqual(self._done(), [False] * 6)
        page = self.client.get("/merchant/")
        self.assertContains(page, "0 dari 6 langkah selesai")
        self.assertContains(page, "Bagikan ke WhatsApp")
        self.assertContains(page, "Semua beres")

    def test_steps_tick_as_the_seller_sets_up(self):
        self.store.storefront_template = "promo"
        self.store.save()
        Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=1)
        StorePaymentSettings.objects.create(store=self.store, bank_transfer_enabled=True,
            bank_name="BCA", bank_account_number="123", bank_account_holder="Toko")
        ShippingMethod.objects.create(store=self.store, name="Reguler", code="reg", flat_rate=1)

        self.assertEqual(self._done(), [True, False, True, True, True, False])

    def test_checklist_disappears_once_everything_is_done(self):
        self.store.storefront_template = "promo"
        self.store.cover_image = "stores/covers/x.png"
        self.store.save()
        Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=1)
        StorePaymentSettings.objects.create(store=self.store, bank_transfer_enabled=True,
            bank_name="BCA", bank_account_number="123", bank_account_holder="Toko")
        ShippingMethod.objects.create(store=self.store, name="Reguler", code="reg", flat_rate=1)
        Order.objects.create(store=self.store, customer_name="Ayu", phone="1",
            shipping_address="x", subtotal=Decimal(1), total=Decimal(1),
            payment_provider="bank_transfer", idempotency_key="first")

        page = self.client.get("/merchant/")
        self.assertNotContains(page, "Siapkan toko Anda")
        self.assertContains(page, "1</b> transfer bank menunggu dicek")

    def test_sidebar_groups_open_on_the_current_section(self):
        page = self.client.get("/merchant/finance/").content.decode()
        finance = page.split("Keuangan</summary>")[0].rsplit("<details", 1)[1]
        self.assertIn("open", finance)
        products = page.split("Produk</summary>")[0].rsplit("<details", 1)[1]
        self.assertNotIn("open", products)
