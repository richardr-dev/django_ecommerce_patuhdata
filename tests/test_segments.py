"""Segmentasi pelanggan, tags and email broadcast with unsubscribe."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.orders.models import Order
from apps.shops.models import Customer, EmailBroadcast, Store
from apps.shops.segments import send_broadcast_batch


@override_settings(PLATFORM_DOMAIN="")
class SegmentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.loyal = Customer.objects.create(store=self.store, name="Sri", phone="081111111111",
            email="sri@example.com", accepts_marketing=True)
        self.quiet = Customer.objects.create(store=self.store, name="Budi", phone="082222222222",
            email="budi@example.com", accepts_marketing=False)
        self.lapsed = Customer.objects.create(store=self.store, name="Ani", phone="083333333333",
            email="ani@example.com", accepts_marketing=True)
        for index in range(3):
            self._order(self.loyal, 100000, f"s{index}")
        old = self._order(self.lapsed, 50000, "a1")
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=90))
        self._order(self.quiet, 999999, "b1", status="cancelled")  # not a sale
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _order(self, customer, total, key, status="paid"):
        return Order.objects.create(store=self.store, customer_name=customer.name,
            phone=customer.phone, shipping_address="x", subtotal=total, total=total,
            status=status, idempotency_key=key)

    def test_filters_use_real_sales(self):
        page = self.seller.get("/merchant/customers/?min_orders=2")
        self.assertContains(page, "Sri")
        self.assertNotContains(page, "Ani")
        self.assertContains(page, "Rp300.000")
        lapsed = self.seller.get("/merchant/customers/?inactive_days=60")
        self.assertContains(lapsed, "Ani")
        self.assertNotContains(lapsed, "<b>Sri</b>")
        self.assertNotContains(self.seller.get("/merchant/customers/?min_spend=500000"), "<b>Budi</b>")

    def test_bulk_tag_then_filter_by_tag(self):
        self.seller.post("/merchant/customers/", {"min_orders": "2", "add_tag": "vip"})
        self.loyal.refresh_from_db()
        self.assertEqual(self.loyal.tag_list, ["vip"])
        self.assertContains(self.seller.get("/merchant/customers/?tag=VIP"), "Sri")
        self.assertNotContains(self.seller.get("/merchant/customers/?tag=vip"), "<b>Ani</b>")

    def test_broadcast_reaches_only_consenting_customers_and_can_be_unsubscribed(self):
        self.seller.post("/merchant/broadcasts/", {"subject": "Promo", "body": "Halo {nama}, diskon 20%!"})
        broadcast = EmailBroadcast.objects.get()
        self.assertEqual(broadcast.recipients, 2)  # Sri and Ani; Budi did not consent
        send_broadcast_batch()
        broadcast.refresh_from_db()
        self.assertEqual((broadcast.status, broadcast.sent_count), ("sent", 2))
        to_sri = next(message for message in mail.outbox if message.to == ["sri@example.com"])
        self.assertIn("Halo Sri, diskon 20%!", to_sri.body)
        link = next(line for line in to_sri.body.splitlines() if "/unsubscribe/" in line)
        path = link[link.index("/unsubscribe/"):].split()[-1] if " " in link else link[link.index("/unsubscribe/"):]
        Client().post(path)
        self.loyal.refresh_from_db()
        self.assertFalse(self.loyal.accepts_marketing)

    def test_empty_audience_is_refused(self):
        reply = self.seller.post("/merchant/broadcasts/?tag=tidak-ada", {"tag": "tidak-ada",
            "subject": "Promo", "body": "Halo semuanya, ada promo"})
        self.assertContains(reply, "Tidak ada penerima")
        self.assertFalse(EmailBroadcast.objects.exists())
