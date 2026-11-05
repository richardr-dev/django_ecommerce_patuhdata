"""Ajak teman: a store invited through lakuin.store/start/?ref=<slug> gets a discounted first
month, and the inviting store gets one once the new store pays its plan."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Plan, Referral, ReferralReward, Subscription, SubscriptionInvoice
from apps.billing.renewals import run_renewals
from apps.billing.services import mark_invoice_paid, start_checkout
from apps.shops.models import Store

SIGNUP = {"store_name": "Toko Baru", "slug": "toko-baru", "phone": "081299990000",
    "full_name": "Sri", "password1": "Sambal-Matah-77", "password2": "Sambal-Matah-77",
    "accept_terms": "on"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, PLATFORM_DOMAIN="",
    REFERRAL_DISCOUNT_PERCENT=50, RENEWAL_NOTICE_DAYS=7)
class ReferralTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.get(is_default=True)
        if not self.plan.price_monthly:
            self.plan.price_monthly = 99000
            self.plan.save(update_fields=["price_monthly"])
        self.price = self.plan.price_monthly
        self.inviter_owner = User.objects.create_user("inviter", password="Sambal-Matah-77")
        self.inviter = Store.objects.create(owner=self.inviter_owner, name="Toko Lama",
            slug="toko-lama", support_phone="081211110000")

    def _signup(self, ref="toko-lama", **extra):
        client = Client()
        client.get(f"/?ref={ref}")  # lands on the platform first, signs up later
        reply = client.post("/start/", {**SIGNUP, **extra})
        self.assertEqual(reply.status_code, 302)
        return Store.objects.get(slug=extra.get("slug", SIGNUP["slug"])), client

    def _pay(self, invoice):
        self.assertEqual(mark_invoice_paid(invoice.id, amount=invoice.amount), "")

    def test_the_invited_store_pays_half_its_first_month(self):
        store, _ = self._signup()
        referral = Referral.objects.get(referred=store)
        self.assertEqual((referral.referrer, referral.status), (self.inviter, "signed_up"))
        invoice = start_checkout(store, self.plan, 1, store.owner)
        self.assertEqual(invoice.discount, self.price / 2)
        self.assertEqual(invoice.amount, self.price - self.price / 2)
        second = start_checkout(store, self.plan, 1, store.owner)  # abandoned the first page
        self.assertEqual(second.discount, self.price / 2)
        self._pay(second)
        reward = ReferralReward.objects.get(store=store)
        self.assertIsNotNone(reward.used_at)
        self.assertEqual(start_checkout(store, self.plan, 1, store.owner).discount, 0)

    def test_the_inviter_earns_a_month_once_the_new_store_pays(self):
        store, _ = self._signup()
        self.assertFalse(ReferralReward.objects.filter(store=self.inviter).exists())
        self._pay(start_checkout(store, self.plan, 1, store.owner))
        referral = Referral.objects.get(referred=store)
        self.assertEqual(referral.status, "qualified")
        self.assertTrue(ReferralReward.objects.filter(store=self.inviter, kind="referrer").exists())
        # The inviter's next renewal bill carries the discount.
        Subscription.objects.update_or_create(store=self.inviter, defaults={"plan": self.plan,
            "current_period_end": timezone.now() + timedelta(days=3)})
        SubscriptionInvoice.objects.create(store=self.inviter, plan=self.plan, amount=self.price,
            provider="demo", status="paid", paid_at=timezone.now() - timedelta(days=30))
        run_renewals()
        renewal = SubscriptionInvoice.objects.get(store=self.inviter, kind="renewal")
        self.assertEqual((renewal.amount, renewal.discount), (self.price / 2, self.price / 2))

    def test_inviting_yourself_earns_nothing(self):
        store, _ = self._signup(phone="0812-1111-0000", slug="toko-kedua", email="kedua@example.com")
        self.assertEqual(Referral.objects.get(referred=store).status, "rejected")
        self.assertFalse(ReferralReward.objects.exists())
        self._pay(start_checkout(store, self.plan, 1, store.owner))
        self.assertFalse(ReferralReward.objects.filter(store=self.inviter).exists())

    def test_unknown_codes_are_ignored(self):
        store, _ = self._signup(ref="tidak-ada")
        self.assertFalse(Referral.objects.filter(referred=store).exists())

    def test_several_rewards_stack_on_a_yearly_bill_but_never_make_it_free(self):
        if not self.plan.price_yearly:
            self.plan.price_yearly = self.price * 10
            self.plan.save(update_fields=["price_yearly"])
        for index in range(30):
            other = Store.objects.create(owner=User.objects.create_user(f"o{index}"),
                name=f"T{index}", slug=f"t-{index}")
            referral = Referral.objects.create(referrer=self.inviter, referred=other,
                status="qualified")
            ReferralReward.objects.create(store=self.inviter, referral=referral, kind="referrer",
                percent=50)
        yearly = start_checkout(self.inviter, self.plan, 12, self.inviter_owner)
        self.assertEqual(yearly.referral_rewards.count(), 12)
        self.assertGreater(yearly.amount, 0)
        self.assertLessEqual(yearly.discount, self.plan.price_yearly * 9 / 10)

    def test_billing_page_shows_the_invite_link_and_the_signup_page_the_offer(self):
        seller = Client()
        seller.force_login(self.inviter_owner)
        page = seller.get("/merchant/billing/")
        self.assertContains(page, "Ajak teman, hemat langganan")
        self.assertContains(page, "/r/toko-lama")
        signup = Client().get("/start/?ref=toko-lama")
        self.assertContains(signup, "Diundang oleh <b>Toko Lama</b>")
        self.assertContains(signup, "diskon 50%")
