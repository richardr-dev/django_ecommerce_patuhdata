"""Short links, kept apart: lakuin.store/r/<slug> invites a seller to Lakuin (Ajak teman), and
lakuin.store/s/<code> is a seller's own campaign link, with clicks, orders and revenue."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import (
    Plan,
    Referral,
    ReferralLink,
    ReferralReward,
    Subscription,
    SubscriptionInvoice,
)
from apps.billing.renewals import run_renewals
from apps.billing.services import mark_invoice_paid, start_checkout
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import Order
from apps.shops.models import CampaignLink, Store

GUEST = {"customer_name": "Ayu", "phone": "0812-3456-7890", "shipping_address": "Jl. Melati 1"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, PLATFORM_DOMAIN="",
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="", REFERRAL_DISCOUNT_PERCENT=50)
class ShortLinkTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller", email="seller@toko.id")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Kaos", slug="kaos",
            sku="KAOS-01", price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="G", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=9)
        self.seller = Client()
        self.seller.force_login(self.owner)

    # Ajak teman (Lakuin referral)
    def test_referral_short_link_counts_and_opens_signup_with_the_invite(self):
        visitor = Client()
        reply = visitor.get("/r/ruuma")
        self.assertTrue(reply["Location"].endswith("/start/?ref=ruuma"))
        self.assertEqual(ReferralLink.objects.get(store=self.store).clicks, 1)
        self.assertContains(visitor.get("/start/"), "Diundang oleh <b>Ruuma</b>")  # remembered
        self.assertTrue(Client().get("/r/tidak-ada")["Location"].endswith("/"))
        page = self.seller.get("/merchant/billing/")
        self.assertContains(page, "/r/ruuma")
        self.assertContains(page, "Link kampanye")  # points sellers to the other tool

    def test_inviter_is_emailed_and_an_open_renewal_takes_the_reward(self):
        plan = Plan.objects.get(is_default=True)
        if not plan.price_monthly:
            plan.price_monthly = 99000
            plan.save(update_fields=["price_monthly"])
        Subscription.objects.update_or_create(store=self.store, defaults={"plan": plan,
            "current_period_end": timezone.now() + timedelta(days=3)})
        SubscriptionInvoice.objects.create(store=self.store, plan=plan, amount=plan.price_monthly,
            provider="demo", status="paid", paid_at=timezone.now() - timedelta(days=30))
        run_renewals()
        renewal = SubscriptionInvoice.objects.get(store=self.store, kind="renewal")
        self.assertEqual(renewal.discount, 0)
        mail.outbox.clear()
        friend = Store.objects.create(owner=User.objects.create_user("friend"), name="Teman",
            slug="teman")
        Subscription.objects.create(store=friend, plan=plan)
        referral = Referral.objects.create(referrer=self.store, referred=friend)
        ReferralReward.objects.create(store=friend, referral=referral, kind="welcome", percent=50)
        with self.captureOnCommitCallbacks(execute=True):
            invoice = start_checkout(friend, plan, 1, friend.owner)
            mark_invoice_paid(invoice.id, amount=invoice.amount)
        renewal.refresh_from_db()
        self.assertEqual(renewal.discount, plan.price_monthly / 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Teman", mail.outbox[0].body)
        self.assertIn("seller@toko.id", mail.outbox[0].to)

    def test_rules_page_and_home_section(self):
        self.assertContains(Client().get("/ajak-teman/"), "Syarat program")

    # Link kampanye (the seller's own store marketing)
    def _create(self, **data):
        return self.seller.post("/merchant/campaign-links/", {"name": "Bio IG", "target": "home",
            **data})

    def test_seller_creates_a_campaign_link_and_orders_are_counted_for_it(self):
        self._create(name="Promo kaos", target="product", product_sku="KAOS-01", code="promo-kaos")
        link = CampaignLink.objects.get(code="promo-kaos")
        self.assertEqual((link.store, link.destination), (self.store, "/products/kaos/"))
        shopper = Client()
        reply = shopper.get("/s/promo-kaos")
        self.assertIn("/products/kaos/?lk=promo-kaos", reply["Location"])
        self.assertIn("utm_campaign=promo-kaos", reply["Location"])
        shopper.get("/products/kaos/?lk=promo-kaos")
        shopper.post(f"/cart/add/{self.product.id}/")
        shopper.post("/checkout/", GUEST)
        order = Order.objects.latest("created_at")
        self.assertEqual(order.campaign_link, link)
        link.refresh_from_db()
        self.assertEqual(link.clicks, 1)
        Order.objects.filter(pk=order.pk).update(status="paid")
        page = self.seller.get("/merchant/campaign-links/")
        self.assertContains(page, "Promo kaos")
        self.assertContains(page, "/s/promo-kaos")
        self.assertContains(page, f"Rp{int(order.total):,}".replace(",", "."))

    def test_codes_are_unique_and_safe(self):
        self._create(code="bio")
        self.assertContains(self._create(code="bio"), "sudah dipakai")
        self.assertContains(self._create(code="Bad Code!"), "huruf kecil")
        self.assertContains(self._create(target="path", path="https://evil.example"), "diawali /")
        self._create(name="Acak")
        self.assertEqual(len(CampaignLink.objects.get(name="Acak").code), 6)

    def test_inactive_links_stop_and_other_stores_are_not_credited(self):
        self._create(code="bio")
        link = CampaignLink.objects.get(code="bio")
        self.seller.post(f"/merchant/campaign-links/{link.id}/toggle/")
        self.assertEqual(Client().get("/s/bio").status_code, 404)
        other = Store.objects.create(owner=User.objects.create_user("o"), name="Lain", slug="lain")
        CampaignLink.objects.create(store=other, name="X", code="lain-x")
        shopper = Client()
        shopper.get("/?lk=lain-x")  # a code of another store does not attach here
        self.assertNotIn("campaign_link", shopper.session)
