"""Proforma from a quote, the buyer's quote link (view, accept, decline), the buyer's "sudah
transfer" on the invoice link, WhatsApp through the store's account, automatic reminders."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting.bank import add_bank_account
from apps.catalog.models import Product
from apps.integrations.models import StoreWhatsAppSettings
from apps.shops.models import Customer, Store
from apps.tax import quotes, reminders, sharing
from apps.tax.models import Quote, ReminderLog, ReminderPolicy
from apps.tax.sales import create_sales_invoice


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.kabel = Product.objects.create(store=self.store, name="Kabel", slug="kabel", sku="KBL", price=100_000)
        self.ani = Customer.objects.create(store=self.store, name="Toko Ani", phone="081234567890", email="ani@contoh.id")
        self.client.force_login(self.owner)
        self.today = timezone.localdate()

    def quote(self):
        return quotes.save_quote(self.store, lines=[{"product": self.kabel, "qty": 2, "unit_price": Decimal(90_000)}],
            buyer_name="Toko Ani", customer=self.ani, phone=self.ani.phone, actor=self.owner)

    def invoice(self, days=7):
        return create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": 1, "unit_price": Decimal(100_000)}],
            buyer_name="Toko Ani", customer=self.ani, phone=self.ani.phone, on_credit=True,
            due_date=self.today + timedelta(days=days))


class ProformaAndQuoteLinkTests(Base):
    def test_the_proforma_prints_from_the_quote_with_how_to_pay(self):
        add_bank_account(self.store, name="Maju Jaya", bank_name="BCA", account_number="1234567")
        quote = self.quote()
        page = self.client.get(reverse("merchant-quote-proforma", args=[quote.id]))
        self.assertContains(page, "PROFORMA INVOICE")
        self.assertContains(page, f"PRO-{quote.number}")
        self.assertContains(page, "Bukan faktur pajak")
        self.assertContains(page, "1234567")
        self.assertContains(page, "180.000")
        self.assertContains(self.client.get(reverse("merchant-quote-detail", args=[quote.id])), "Proforma")

    def test_the_buyer_opens_the_link_and_accepts(self):
        quote = self.quote()
        url = reverse("quote-public", args=[sharing.ensure_quote_token(quote)])
        buyer = Client()
        page = buyer.get(url)
        self.assertContains(page, "Setujui penawaran")
        quote.refresh_from_db()
        self.assertIsNotNone(quote.viewed_at)
        reply = buyer.post(url + "respond/", {"action": "accept", "note": "Kirim minggu depan"})
        self.assertRedirects(reply, url + "?ok=accept", fetch_redirect_response=False)
        quote.refresh_from_db()
        self.assertEqual((quote.status, quote.response_note), ("accepted", "Kirim minggu depan"))
        self.assertEqual(buyer.post(url + "respond/", {"action": "decline"}).status_code, 404)  # answered already
        self.assertContains(self.client.get(reverse("merchant-quote-detail", args=[quote.id])), "Kirim minggu depan")

    def test_an_expired_quote_cannot_be_answered_and_a_short_token_is_404(self):
        quote = self.quote()
        Quote.objects.filter(pk=quote.pk).update(valid_until=self.today - timedelta(days=1))
        url = reverse("quote-public", args=[sharing.ensure_quote_token(quote)])
        self.assertContains(Client().get(url), "sudah lewat")
        self.assertEqual(Client().post(url + "respond/", {"action": "accept"}).status_code, 404)
        self.assertEqual(Client().get("/q/short/").status_code, 404)

    def test_the_seller_sends_the_quote_by_email_and_marks_whatsapp(self):
        quote = self.quote()
        self.client.post(reverse("merchant-quote-email", args=[quote.id]), {"to": "ani@contoh.id", "note": "Halo Bu"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/q/", mail.outbox[0].body)
        self.assertIn("Halo Bu", mail.outbox[0].body)
        quote.refresh_from_db()
        self.assertEqual(quote.status, "sent")
        self.assertIsNotNone(quote.sent_at)
        self.assertEqual(self.client.post(reverse("merchant-quote-sent", args=[quote.id])).status_code, 204)


class BuyerTransferClaimTests(Base):
    def test_the_buyer_says_they_paid_and_the_seller_sees_it_on_beranda(self):
        invoice = self.invoice()
        url = reverse("invoice-public", args=[sharing.ensure_token(invoice)])
        buyer = Client()
        self.assertContains(buyer.get(url), "Saya sudah transfer")
        reply = buyer.post(url + "konfirmasi/", {"bank": "BCA", "date": self.today.isoformat(), "amount": "100000"})
        self.assertRedirects(reply, url + "?ok=transfer", fetch_redirect_response=False)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.transfer_claimed_at)
        self.assertIn("BCA", invoice.transfer_claim_note)
        self.assertContains(self.client.get("/books/"), "konfirmasi transfer")
        self.assertContains(self.client.get(reverse("merchant-invoice-detail", args=[invoice.id])), "sudah transfer")


class WhatsAppAccountTests(Base):
    def test_without_an_account_the_button_explains_and_with_one_the_message_is_sent(self):
        invoice = self.invoice()
        reply = self.client.post(reverse("merchant-invoice-whatsapp", args=[invoice.id]), follow=True)
        self.assertContains(reply, "WhatsApp belum terhubung")
        account = StoreWhatsAppSettings.objects.create(store=self.store, provider="fonnte")
        account.token = "abc"
        account.save()
        with mock.patch("apps.integrations.whatsapp.sender_for") as sender:
            sender.return_value.send.return_value = "msg-1"
            reply = self.client.post(reverse("merchant-invoice-whatsapp", args=[invoice.id]), follow=True)
        self.assertContains(reply, "dikirim lewat WhatsApp ke 6281234567890")
        to, text = sender.return_value.send.call_args[0]
        self.assertEqual(to, "6281234567890")
        self.assertIn(invoice.number, text)
        self.assertIn("/i/", text)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.sent_at)


class AutomaticReminderTests(Base):
    def test_the_policy_decides_what_goes_out_and_each_step_goes_once(self):
        self.assertEqual(reminders.due_today(self.store, self.today), [])      # off by default
        ReminderPolicy.objects.create(store=self.store, enabled=True, days_before=3, days_after="1,7")
        soon = self.invoice(days=3)
        late = self.invoice(days=-7)
        self.invoice(days=-2)                                                   # no step today
        due = {(inv.pk, kind, offset) for inv, kind, offset in reminders.due_today(self.store, self.today)}
        self.assertEqual(due, {(soon.pk, "before", 3), (late.pk, "after", 7)})
        sent = reminders.run(self.store, self.today)
        self.assertEqual(sent, 2)                                               # email only: no WhatsApp account
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("3 hari", next(m.body for m in mail.outbox if soon.number in m.body))
        self.assertIn("lewat jatuh tempo 7 hari", next(m.body for m in mail.outbox if late.number in m.body))
        self.assertEqual(reminders.run(self.store, self.today), 0)             # never twice
        self.assertEqual(ReminderLog.objects.filter(invoice=late, status="sent").count(), 1)
        self.assertContains(self.client.get(reverse("merchant-invoice-detail", args=[late.id])), "Pengingat otomatis")

    def test_the_policy_is_saved_from_the_receivables_page(self):
        self.client.post(reverse("merchant-reminder-policy"), {"enabled": "1", "days_before": "5",
            "days_after": "2, 10, x, 400", "use_email": "1"})
        policy = ReminderPolicy.objects.get(store=self.store)
        self.assertEqual((policy.enabled, policy.days_before, policy.days_after, policy.use_email, policy.use_whatsapp),
            (True, 5, "2,10", True, False))
        self.assertContains(self.client.get(reverse("merchant-receivables")), "Pengingat otomatis")
