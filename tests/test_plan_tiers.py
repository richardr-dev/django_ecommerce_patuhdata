"""Subscription tiers from Starter to Custom, and the limits each one sets."""
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from test_platform import PLATFORM, _store

from apps.billing.models import Plan
from apps.billing.plans import effective_plan
from apps.billing.services import BillingError, start_checkout
from apps.channels.models import SalesChannel


class TierCatalogTests(TestCase):
    def test_public_tiers_run_from_starter_to_custom(self):
        tiers = list(Plan.objects.filter(is_public=True).values_list("name", flat=True))
        self.assertEqual(tiers, ["Starter", "Growth", "Pro", "Business", "Custom"])
        self.assertEqual(Plan.objects.get(is_default=True).name, "Starter")

    def test_each_paid_tier_lifts_the_limits_of_the_one_below(self):
        starter, growth, pro, business = (Plan.objects.get(code=code)
            for code in ("free", "growth", "pro", "business"))

        self.assertLess(starter.price_monthly, growth.price_monthly)
        self.assertLess(growth.price_monthly, pro.price_monthly)
        self.assertLess(pro.price_monthly, business.price_monthly)
        self.assertEqual((starter.max_channels, growth.max_channels, pro.max_channels),
            (1, 3, 10))
        self.assertIsNone(business.max_channels)
        self.assertEqual([plan.allow_payout_reconciliation for plan in (starter, growth, pro)],
            [False, False, True])

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

        response = self._client(store).post("/merchant/marketplace/", {"platform": "tokopedia",
            "name": "Tokopedia"})
        self.assertContains(response, "maksimal 1 toko marketplace")
        self.assertEqual(store.sales_channels.count(), 1)

        growth_store = _store("growth", plan=Plan.objects.get(code="growth"))
        SalesChannel.objects.create(store=growth_store, platform="shopee", name="Shopee")
        self._client(growth_store).post("/merchant/marketplace/", {"platform": "tokopedia",
            "name": "Tokopedia"})
        self.assertEqual(growth_store.sales_channels.count(), 2)

    def test_payout_reconciliation_starts_at_pro(self):
        store = _store("hemat", plan=Plan.objects.get(code="growth"))
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
        self.assertContains(response, "1 toko marketplace")
        self.assertContains(response, "<dd>0 / 1</dd>")


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}"],
    PLATFORM_SALES_URL="https://wa.me/6281200000000")
class PricingPageTests(TestCase):
    def test_platform_home_lists_every_tier_with_a_sales_contact_for_custom(self):
        response = Client(HTTP_HOST=PLATFORM).get("/")

        for name in ("Starter", "Growth", "Pro", "Business", "Custom"):
            self.assertContains(response, f"<h3>{name}</h3>")
        self.assertContains(response, "https://wa.me/6281200000000")
        self.assertContains(response, "Paling populer", count=1)
