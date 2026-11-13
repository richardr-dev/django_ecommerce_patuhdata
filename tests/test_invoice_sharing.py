"""Send an invoice: a secret link the buyer opens, a WhatsApp message, an email, a reminder."""
import datetime
from decimal import Decimal

from django.core import mail
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import bank
from apps.tax import sharing
from apps.tax.models import Invoice


class SharingTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.inv = self.invoice(kabel=2, credit=True, due_days=14)
        self.inv.order.phone = "081234567890"
        self.inv.order.save(update_fields=["phone"])

    def public(self):
        return reverse("invoice-public", args=[sharing.ensure_token(self.inv)])

    def test_buyer_opens_the_link_without_logging_in_and_it_is_recorded(self):
        url = self.public()
        self.client.logout()
        page = self.client.get(url)
        self.assertContains(page, self.inv.number)
        self.assertContains(page, "Sisa tagihan")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.viewed_at)

    def test_seller_previewing_does_not_count_as_viewed(self):
        self.client.get(self.public())
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.viewed_at)

    def test_wrong_or_draft_token_is_not_found(self):
        self.assertEqual(self.client.get(reverse("invoice-public", args=["x" * 30])).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoice-public", args=["short"])).status_code, 404)
        Invoice.objects.filter(pk=self.inv.pk).update(status=Invoice.STATUS_DRAFT)
        self.assertEqual(self.client.get(self.public()).status_code, 404)

    def test_page_shows_bank_accounts_to_pay_into(self):
        account = bank.bank_accounts(self.store)[0]
        account.bank_name, account.account_number = "BCA", "123-456-7890"
        account.save()
        self.client.logout()
        self.assertContains(self.client.get(self.public()), "123-456-7890")

    def test_paid_invoice_shows_lunas_not_how_to_pay(self):
        paid = self.invoice(kabel=1, credit=False)
        token = sharing.ensure_token(paid)
        self.client.logout()
        page = self.client.get(reverse("invoice-public", args=[token]))
        self.assertContains(page, "Lunas")
        self.assertNotContains(page, "Cara membayar")

    def test_message_is_an_invoice_then_a_reminder_when_late(self):
        text = sharing.message_text(self.inv, "https://x/i/t/", Decimal(222_000))
        self.assertIn("Rp222.000", text)
        self.assertNotIn("mengingatkan", text)
        Invoice.objects.filter(pk=self.inv.pk).update(due_date=self.today - datetime.timedelta(days=5))
        self.inv.refresh_from_db()
        late = sharing.message_text(self.inv, "https://x/i/t/", Decimal(222_000))
        self.assertIn("mengingatkan", late)
        self.assertIn("5 hari", late)
        self.assertIn("terima kasih", sharing.message_text(self.inv, "u", Decimal(0)).lower())

    def test_whatsapp_link_uses_the_buyers_number(self):
        self.assertTrue(sharing.whatsapp_link("0812-3456-7890", "hi").startswith("https://wa.me/6281234567890?text="))
        page = self.client.get(reverse("merchant-invoice-detail", args=[self.inv.pk]))
        self.assertContains(page, "https://wa.me/6281234567890")
        self.assertContains(page, "Kirim lewat WhatsApp")

    def test_email_is_sent_and_recorded(self):
        response = self.client.post(reverse("merchant-invoice-email", args=[self.inv.pk]),
            {"to": "budi@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["budi@example.com"])
        self.assertIn("/i/", mail.outbox[0].body)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.sent_at)

    def test_bad_email_is_refused_and_nothing_is_sent(self):
        self.client.post(reverse("merchant-invoice-email", args=[self.inv.pk]), {"to": "nope"})
        self.assertEqual(len(mail.outbox), 0)

    def test_mark_sent_endpoint(self):
        response = self.client.post(reverse("merchant-invoice-sent", args=[self.inv.pk]))
        self.assertEqual(response.status_code, 204)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.sent_at)


class DeliveryNoteTests(SharingTests):
    def test_surat_jalan_lists_goods_without_prices(self):
        page = self.client.get(reverse("merchant-delivery-note", args=[self.inv.pk]))
        self.assertContains(page, "SURAT JALAN")
        self.assertContains(page, "Kabel")
        self.assertContains(page, "Penerima")
        self.assertNotContains(page, "100.000")
        self.assertNotContains(page, "Rp")

    def test_not_for_drafts_or_other_stores(self):
        Invoice.objects.filter(pk=self.inv.pk).update(status=Invoice.STATUS_DRAFT)
        self.assertEqual(self.client.get(reverse("merchant-delivery-note", args=[self.inv.pk])).status_code, 404)

    def test_menu_links_to_it(self):
        self.assertContains(self.client.get(reverse("merchant-invoice-detail", args=[self.inv.pk])),
            reverse("merchant-delivery-note", args=[self.inv.pk]))


class ClosedStoreTests(SharingTests):
    def test_invoice_link_opens_even_when_the_storefront_is_gated(self):
        from django.test import override_settings
        url = self.public()
        self.client.logout()
        with override_settings(STOREFRONT_GATE=True):
            self.assertEqual(self.client.get(url).status_code, 200)


class AbShopInvoiceTests(SharingTests):
    def setUp(self):
        super().setUp()
        self.kabel.ab_shop_price = Decimal(70_000)
        self.kabel.save()

    def post_invoice(self, **extra):
        data = {"buyer_name": "AB Shop", "issued_on": self.today.isoformat(), "payment_status": "credit",
            "channel": "abshop", "product[]": [str(self.kabel.pk)], "description[]": [""], "qty[]": ["3"],
            "unit_price[]": [""], "line_discount[]": [""], "unit[]": [""], "note[]": [""], "code[]": [""],
            "shipping": "25000", "courier": "JNE", "tracking_number": "JNE0001", "ab_shop_source": "shopee",
            **extra}
        return self.client.post(reverse("merchant-invoice-new"), data)

    def test_a_blank_price_becomes_the_ab_shop_price_with_shipping_and_courier(self):
        self.assertEqual(self.post_invoice().status_code, 302)
        invoice = Invoice.objects.latest("issued_on", "created_at")
        order = invoice.order
        self.assertEqual(invoice.items.get().unit_price, Decimal(70_000))
        self.assertEqual((order.channel, order.ab_shop_source, order.abshop_billing_status),
            ("abshop", "shopee", "billed"))
        self.assertEqual((order.courier_code, order.tracking_number), ("jne", "JNE0001"))
        self.assertEqual(invoice.subtotal, Decimal(210_000))
        self.assertEqual(invoice.shipping, Decimal(25_000))
        self.assertEqual(invoice.total, Decimal(210_000) * Decimal("1.11") + Decimal(25_000))

    def test_a_typed_price_is_kept_and_other_channels_use_the_normal_price(self):
        self.post_invoice(**{"unit_price[]": ["65000"]})
        self.assertEqual(Invoice.objects.latest("created_at").items.get().unit_price, Decimal(65_000))
        self.post_invoice(channel="manual")
        self.assertEqual(Invoice.objects.latest("created_at").items.get().unit_price, Decimal(0))

    def test_courier_and_resi_show_on_every_document(self):
        self.post_invoice()
        invoice = Invoice.objects.latest("created_at")
        detail = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk])).content.decode()
        public = self.client.get(reverse("invoice-public", args=[sharing.ensure_token(invoice)])).content.decode()
        for html in (detail, public):
            self.assertIn("JNE", html)
            self.assertIn("JNE0001", html)
        self.assertIn("Shopee AB Shop", detail)
        from django.template.loader import render_to_string
        self.assertIn("Resi JNE0001", render_to_string("merchant/invoice_pdf.html", {"invoices": [invoice]}))
        self.assertIn("Resi JNE0001", render_to_string("tax/invoice.html", {"invoice": invoice}))

    def test_form_offers_the_ab_shop_price_and_source(self):
        page = self.client.get(reverse("merchant-invoice-new"))
        self.assertContains(page, "AB Shop menjual lewat")
        self.assertContains(page, 'name="courier"')
        catalog = {c["sku"]: c for c in page.context["catalog"]}
        self.assertEqual(catalog["KBL"]["ab_price"], "70000")
        self.assertEqual(catalog["LMP"]["ab_price"], "")

    def test_ab_shop_orders_reach_the_partner_page_and_billing_queue(self):
        self.post_invoice()
        page = self.client.get(reverse("merchant-abshop"))
        self.assertContains(page, "AB Shop")
        self.assertEqual(page.context["summary"]["total"], 1)
        self.assertEqual(page.context["summary"]["unbilled"], 0)
