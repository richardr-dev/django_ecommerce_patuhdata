"""Monthly renewal billing: bill ahead, remind, pay in one click, extend without losing days,
and a short grace period for paying sellers."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Plan, Subscription, SubscriptionCheckout, SubscriptionInvoice
from apps.billing.plans import effective_plan
from apps.billing.renewals import open_payment_page, run_renewals
from apps.billing.services import check_pending_invoice, handle_doku_notification, mark_invoice_paid
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, RENEWAL_NOTICE_DAYS=7,
    RENEWAL_GRACE_DAYS=3)
class RenewalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", email="owner@toko.id")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.starter = Plan.objects.get(code="free")
        self.end = timezone.now() + timedelta(days=6, hours=12)
        self.subscription = Subscription.objects.create(store=self.store, plan=self.starter,
            current_period_end=self.end)

    def _pay_once_before(self):
        """Mark the store as a paying customer (not a trial)."""
        SubscriptionInvoice.objects.create(store=self.store, plan=self.starter, amount=99000,
            provider="demo", status="paid", paid_at=timezone.now() - timedelta(days=30))

    def test_bills_seven_days_ahead_and_emails_once(self):
        run_renewals()
        run_renewals()

        invoice = SubscriptionInvoice.objects.get(kind="renewal")
        self.assertEqual((invoice.amount, invoice.months, invoice.due_at), (99000, 1, self.end))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("owner@toko.id", mail.outbox[0].to)
        self.assertIn("Rp99.000", mail.outbox[0].body)

    def test_reminders_escalate_toward_and_past_the_end(self):
        for days_before in (6.5, 2.5, 0.5, -0.5, -3.5):
            run_renewals(now=self.end - timedelta(days=days_before))
        self.assertEqual(len(mail.outbox), 5)
        self.assertEqual(SubscriptionInvoice.objects.filter(kind="renewal").count(), 1)

    def test_nothing_is_billed_too_early_or_when_auto_renew_is_off(self):
        run_renewals(now=self.end - timedelta(days=10))
        self.subscription.auto_renew = False
        self.subscription.save()
        run_renewals()
        self.assertFalse(SubscriptionInvoice.objects.exists())

    def test_paying_extends_from_the_period_end_and_remembers_the_period(self):
        run_renewals()
        invoice = SubscriptionInvoice.objects.get(kind="renewal")
        self.assertEqual(mark_invoice_paid(invoice.id, amount=99000), "")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.current_period_end, self.end + timedelta(days=30))
        self.assertEqual(self.subscription.renew_months, 1)

    def test_a_manual_purchase_settles_the_open_renewal_bill(self):
        run_renewals()
        manual = SubscriptionInvoice.objects.create(store=self.store, plan=self.starter,
            months=12, amount=990000, provider="demo")
        mark_invoice_paid(manual.id, amount=990000)

        self.assertEqual(SubscriptionInvoice.objects.get(kind="renewal").status, "cancelled")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.renew_months, 12)

    def test_paying_seller_keeps_the_plan_through_the_grace_period_but_a_trial_does_not(self):
        Subscription.objects.filter(id=self.subscription.id).update(
            current_period_end=timezone.now() - timedelta(days=1))
        self.store.refresh_from_db()
        self.assertEqual(effective_plan(self.store).code, "lapsed")  # trial: no grace

        self._pay_once_before()
        self.store.refresh_from_db()
        self.assertEqual(effective_plan(self.store), self.starter)

        Subscription.objects.filter(id=self.subscription.id).update(
            current_period_end=timezone.now() - timedelta(days=4))
        self.store.refresh_from_db()
        self.assertEqual(effective_plan(self.store).code, "lapsed")

    @override_settings(PAYMENT_PROVIDER="doku", DOKU_CLIENT_ID="MCH", DOKU_SECRET_KEY="secret")
    @patch("apps.billing.renewals.DokuPaymentProvider.create_checkout",
        side_effect=["https://doku.test/1", "https://doku.test/2"])
    def test_each_click_opens_a_fresh_page_and_an_older_page_still_pays(self, create_checkout):
        run_renewals()
        invoice = SubscriptionInvoice.objects.get(kind="renewal")
        first, second = open_payment_page(invoice, self.owner), open_payment_page(invoice, self.owner)

        self.assertEqual((first, second), ("https://doku.test/1", "https://doku.test/2"))
        old_reference = SubscriptionCheckout.objects.order_by("id").first().reference
        _, status = handle_doku_notification(old_reference, "notif-1",
            {"order": {"amount": 99000, "currency": "IDR"}})
        invoice.refresh_from_db()
        self.assertEqual((status, invoice.status), (200, "paid"))

    @patch("apps.billing.services.DokuPaymentProvider.fetch_status")
    def test_an_unpaid_renewal_bill_is_not_cancelled_after_an_hour(self, fetch_status):
        fetch_status.return_value.state = "unpaid"
        run_renewals()
        invoice = SubscriptionInvoice.objects.get(kind="renewal")
        SubscriptionInvoice.objects.filter(id=invoice.id).update(provider="doku",
            created_at=timezone.now() - timedelta(days=2))
        invoice.refresh_from_db()

        self.assertEqual(check_pending_invoice(invoice), "pending")

    def test_email_failure_is_retried_next_run(self):
        with patch("apps.billing.renewals.send_mail", side_effect=OSError("smtp down")):
            run_renewals()
        self.assertEqual(SubscriptionInvoice.objects.get(kind="renewal").reminders_sent, 0)
        run_renewals()
        self.assertEqual(len(mail.outbox), 1)


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True)
class RenewalPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", email="owner@toko.id")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.plan = Plan.objects.get(code="free")
        self.subscription = Subscription.objects.create(store=self.store, plan=self.plan,
            current_period_end=timezone.now() + timedelta(days=5))
        run_renewals()
        self.client = Client()
        self.client.force_login(self.owner)

    def test_billing_page_shows_the_bill_and_pay_opens_checkout(self):
        page = self.client.get("/merchant/billing/")
        self.assertContains(page, "TAGIHAN BERIKUTNYA")
        invoice = SubscriptionInvoice.objects.get(kind="renewal")

        response = self.client.post(f"/merchant/billing/{invoice.id}/pay/")
        self.assertRedirects(response, f"/merchant/billing/{invoice.id}/demo-pay/",
            fetch_redirect_response=False)
        self.client.post(f"/merchant/billing/{invoice.id}/demo-pay/")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "paid")

    def test_dashboard_points_to_the_open_bill(self):
        self.assertContains(self.client.get("/merchant/"), "Tagihan Starter Rp99.000")

    def test_switching_to_yearly_updates_the_open_bill_and_turning_off_cancels_it(self):
        self.client.post("/merchant/billing/auto-renew/", {"auto_renew": "on", "renew_months": "12"})
        invoice = SubscriptionInvoice.objects.get(kind="renewal")
        self.assertEqual((invoice.months, invoice.amount), (12, 990000))

        self.client.post("/merchant/billing/auto-renew/", {"renew_months": "12"})
        invoice.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertEqual((invoice.status, self.subscription.auto_renew), ("cancelled", False))
