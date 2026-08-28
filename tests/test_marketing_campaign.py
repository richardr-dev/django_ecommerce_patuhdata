from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orders.forms import MarketingCampaignForm
from apps.shops.models import MarketingCampaign, Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class MarketingCampaignTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("campaign-owner")
        self.store = Store.objects.create(owner=self.owner, name="Campaign Shop", slug="campaign-shop")
        self.now = timezone.now()

    def test_active_campaign_renders_timer_popup_and_voucher(self):
        campaign = MarketingCampaign.objects.create(store=self.store, name="Weekend",
            headline="Flash sale akhir pekan", message="Diskon selama persediaan masih ada",
            coupon_code="FLASH20", button_label="Belanja promo", link_url="/#catalog",
            show_countdown=True, show_popup=True, starts_at=self.now - timedelta(hours=1),
            ends_at=self.now + timedelta(hours=4))
        response = self.client.get("/")
        self.assertContains(response, "flash-sale-bar")
        self.assertContains(response, "campaign-popup")
        self.assertContains(response, "FLASH20")
        self.assertContains(response, f'data-campaign="{campaign.id}"')

    def test_future_expired_and_disabled_campaigns_are_not_rendered(self):
        cases = [
            (self.now + timedelta(hours=1), self.now + timedelta(hours=2), True),
            (self.now - timedelta(hours=2), self.now - timedelta(hours=1), True),
            (self.now - timedelta(hours=1), self.now + timedelta(hours=1), False),
        ]
        for index, (starts, ends, active) in enumerate(cases):
            MarketingCampaign.objects.create(store=self.store, name=f"Hidden {index}",
                headline=f"Hidden headline {index}", starts_at=starts, ends_at=ends, is_active=active)
        response = self.client.get("/")
        self.assertNotContains(response, "Hidden headline")

    def test_campaign_requires_end_after_start_and_safe_link(self):
        form = MarketingCampaignForm(data={"name": "Invalid", "headline": "Invalid",
            "starts_at": "2026-08-27T12:00", "ends_at": "2026-08-27T11:00",
            "link_url": "javascript:alert(1)", "position": 0, "is_active": "on"}, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("ends_at", form.errors)
        self.assertIn("link_url", form.errors)
