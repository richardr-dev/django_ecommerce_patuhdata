"""Ajak teman: a store invited through lakuin.store/start/?ref=<slug> gets a discounted first
month, and the inviting store gets one once the new store pays its plan."""

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.billing.models import Plan, Referral, ReferralReward
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


    def _invite_and_pay(self, index):
        owner = User.objects.create_user(f"invited-{index}")
        store = Store.objects.create(owner=owner, name=f"Undangan {index}", slug=f"undangan-{index}")
        Referral.objects.create(referrer=self.inviter, referred=store, status="signed_up")
        self._pay(start_checkout(store, self.plan, 1, owner))
        return ReferralReward.objects.get(referral__referred=store, kind="referrer")



    def test_a_store_without_paid_invites_has_no_tier_yet(self):
        from apps.billing.referrals import referral_summary, tier_for

        self.assertIsNone(referral_summary(self.inviter)["tier"])
        self.assertEqual(referral_summary(self.inviter)["next_tier"]["name"], "Teman")
        self.assertEqual(tier_for(10)[0]["name"], "Duta Patuhdata")
        self.assertIsNone(tier_for(25)[1])
