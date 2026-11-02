"""Three plans on sale (Starter, Pro, Custom), the limits each sets, and the signup trial."""
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from test_platform import PLATFORM, _store

from apps.billing.models import Plan
from apps.billing.plans import effective_plan
from apps.billing.services import BillingError, start_checkout
from apps.channels.models import SalesChannel


class TierCatalogTests(TestCase):
    def test_three_plans_are_on_sale(self):
        tiers = list(Plan.objects.filter(is_public=True).values_list("name", flat=True))
        self.assertEqual(tiers, ["Starter", "Pro", "Custom"])
        self.assertEqual(Plan.objects.get(is_default=True).name, "Starter")

    def test_starter_99k_and_pro_399k_with_pro_unlimited(self):
        starter, pro = Plan.objects.get(code="free"), Plan.objects.get(code="pro")

        self.assertEqual((starter.price_monthly, starter.price_yearly), (99000, 990000))
        self.assertEqual((pro.price_monthly, pro.price_yearly), (399000, 3990000))
        self.assertEqual((starter.max_products, starter.max_channels), (500, 2))
        self.assertEqual((pro.max_products, pro.max_channels), (None, None))
        self.assertTrue(starter.allow_custom_domain)
        self.assertEqual((starter.allow_payout_reconciliation, pro.allow_payout_reconciliation),
            (False, True))

    def test_custom_is_contact_only_and_never_expires_once_assigned(self):
        custom = Plan.objects.get(code="custom")
        store = _store("besar", plan=custom)

        with self.assertRaises(BillingError):
            start_checkout(store, custom, 1, store.owner)
        self.assertEqual(effective_plan(store), custom)
        self.assertIsNone(custom.max_products)


class TierLimitTests(TestCase):
    def _client(self, store):
        client = Client()
        client.force_login(store.owner)
        return client

    def test_marketplace_shops_are_capped_by_the_plan(self):
        store = _store("starter", plan=Plan.objects.get(code="free"))
        SalesChannel.objects.create(store=store, platform="shopee", name="Shopee")
        SalesChannel.objects.create(store=store, platform="lazada", name="Lazada")

        response = self._client(store).post("/merchant/marketplace/", {"platform": "tokopedia",
            "name": "Tokopedia"})
        self.assertContains(response, "maksimal 2 toko marketplace")
        self.assertEqual(store.sales_channels.count(), 2)

        growth_store = _store("growth", plan=Plan.objects.get(code="pro"))
        SalesChannel.objects.create(store=growth_store, platform="shopee", name="Shopee")
        self._client(growth_store).post("/merchant/marketplace/", {"platform": "tokopedia",
            "name": "Tokopedia"})
        self.assertEqual(growth_store.sales_channels.count(), 2)

    def test_payout_reconciliation_starts_at_pro(self):
        store = _store("hemat", plan=Plan.objects.get(code="free"))
        upload = {"provider": "xendit", "file": SimpleUploadedFile("p.csv",
            b"payout_reference,payout_date,order_reference,amount\nPO,2026-11-02,NK,1\n")}

        page = self._client(store).post("/merchant/payouts/", upload)
        self.assertContains(page, "tersedia mulai paket Pro")
        self.assertIsNone(page.context["summary"])

        pro_store = _store("lengkap", plan=Plan.objects.get(code="pro"))
        upload["file"].seek(0)
        page = self._client(pro_store).post("/merchant/payouts/", upload)
        self.assertEqual(len(page.context["summary"]["errors"]), 1)  # NK is not a real order

    @override_settings(PLATFORM_SALES_URL="https://wa.me/6281200000000")
    def test_seller_billing_page_shows_usage_against_limits(self):
        store = _store("pakai", plan=Plan.objects.get(code="free"))
        client = Client()
        client.force_login(User.objects.get(id=store.owner_id))

        response = client.get("/merchant/billing/")
        self.assertContains(response, "Hubungi sales")
        self.assertContains(response, "2 toko marketplace")
        self.assertContains(response, "<dd>0 / 2</dd>")
        self.assertContains(response, "Masa coba gratis")


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}"],
    PLATFORM_SALES_URL="https://wa.me/6281200000000")
class PricingPageTests(TestCase):
    def test_platform_home_lists_every_tier_with_a_sales_contact_for_custom(self):
        response = Client(HTTP_HOST=PLATFORM).get("/")

        for name in ("Starter", "Pro", "Custom"):
            self.assertContains(response, f"<h3>{name}</h3>")
        for hidden in ("Growth", "Business"):
            self.assertNotContains(response, f"<h3>{hidden}</h3>")
        self.assertContains(response, "Rp99.000")
        self.assertContains(response, "Rp399.000")
        self.assertContains(response, "https://wa.me/6281200000000")
        self.assertContains(response, "Paling populer", count=1)


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}"],
    PUBLIC_BASE_URL=f"https://{PLATFORM}", TRIAL_DAYS=14)
class TrialTests(TestCase):
    def test_signup_starts_a_14_day_starter_trial(self):
        from datetime import timedelta

        import test_platform
        from django.utils import timezone

        from apps.shops.models import Store

        Client(HTTP_HOST=PLATFORM).post("/start/", test_platform.SignupTests.form)
        subscription = Store.objects.get(slug="batik-sari").subscription
        self.assertEqual(subscription.plan.name, "Starter")
        self.assertAlmostEqual(subscription.current_period_end,
            timezone.now() + timedelta(days=14), delta=timedelta(minutes=1))

    def test_after_the_trial_the_store_stays_but_cannot_add_products(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.billing.models import Subscription
        from apps.billing.plans import product_limit_error

        store = _store("habis", plan=Plan.objects.get(code="free"))
        Subscription.objects.filter(store=store).update(
            current_period_end=timezone.now() - timedelta(minutes=1))
        store.refresh_from_db()

        self.assertEqual(effective_plan(store).code, "lapsed")
        self.assertIn("maksimal 0", product_limit_error(store, 1))
        client = Client(HTTP_HOST=PLATFORM)
        client.force_login(store.owner)
        self.assertContains(client.get("/merchant/"), "sudah berakhir")
