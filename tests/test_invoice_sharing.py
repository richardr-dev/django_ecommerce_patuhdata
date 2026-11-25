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


class MitraInvoiceTests(SharingTests):
    """Mitra Jaya is a mitra (a ConsignmentPartner): its own price list, its own contact, one invoice form."""

    def setUp(self):
        super().setUp()
        from apps.settlements.models import ConsignmentPartner, PartnerProductPrice
        self.mitra = ConsignmentPartner.objects.create(store=self.store, name="Mitra Jaya",
            contact_phone="0811999", payment_term_days=14)
        self.other = ConsignmentPartner.objects.create(store=self.store, name="Mitra Lain")
        PartnerProductPrice.objects.create(partner=self.mitra, product=self.kabel, price=Decimal(70_000))
        PartnerProductPrice.objects.create(partner=self.other, product=self.kabel, price=Decimal(90_000))

    def post_invoice(self, partner=None, **extra):
        data = {"buyer_name": "Mitra Jaya", "issued_on": self.today.isoformat(), "payment_status": "credit",
            "channel": "manual", "partner_id": str((partner or self.mitra).pk),
            "product[]": [str(self.kabel.pk)], "description[]": [""], "qty[]": ["3"],
            "unit_price[]": [""], "line_discount[]": [""], "unit[]": [""], "note[]": [""], "code[]": [""],
            "shipping": "25000", "courier": "JNE", "tracking_number": "JNE0001", **extra}
        return self.client.post(reverse("merchant-invoice-new"), data)

    def test_the_mitras_own_price_list_prices_a_blank_line_with_shipping_and_courier(self):
        self.assertEqual(self.post_invoice().status_code, 302)
        invoice = Invoice.objects.latest("created_at")
        order = invoice.order
        self.assertEqual(invoice.items.get().unit_price, Decimal(70_000))   # not the other mitra's 90.000
        self.assertEqual(order.consignment_partner_id, self.mitra.pk)
        self.assertEqual((order.order_type, order.channel, order.abshop_billing_status),
            ("reseller_sell_in", "abshop", "billed"))
        self.assertEqual((order.courier_code, order.tracking_number), ("jne", "JNE0001"))
        self.assertEqual(invoice.subtotal, Decimal(210_000))
        self.assertEqual(invoice.shipping, Decimal(25_000))
        self.assertEqual(invoice.total, Decimal(210_000) * Decimal("1.11") + Decimal(25_000))

    def test_each_mitra_gets_its_own_price_and_a_typed_price_wins(self):
        self.post_invoice(partner=self.other)
        self.assertEqual(Invoice.objects.latest("created_at").items.get().unit_price, Decimal(90_000))
        self.post_invoice(**{"unit_price[]": ["65000"]})
        self.assertEqual(Invoice.objects.latest("created_at").items.get().unit_price, Decimal(65_000))

    def test_no_mitra_means_the_normal_flow(self):
        self.post_invoice(partner_id="", **{"unit_price[]": ["100000"]})
        order = Invoice.objects.latest("created_at").order
        self.assertIsNone(order.consignment_partner_id)
        self.assertEqual(order.order_type, "direct")

    def test_form_lists_mitra_prices_and_hides_ab_shop_as_a_channel(self):
        page = self.client.get(reverse("merchant-invoice-new"))
        self.assertContains(page, "Jual ke mitra")
        self.assertContains(page, 'name="courier"')
        self.assertNotContains(page, "Mitra / Reseller")  # not a channel any more
        catalog = {c["sku"]: c for c in page.context["catalog"]}
        self.assertEqual(catalog["KBL"]["mitra"], {str(self.mitra.pk): "70000", str(self.other.pk): "90000"})
        self.assertEqual(catalog["LMP"]["mitra"], {})

    def test_courier_and_resi_show_on_every_document(self):
        self.post_invoice()
        invoice = Invoice.objects.latest("created_at")
        detail = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk])).content.decode()
        public = self.client.get(reverse("invoice-public", args=[sharing.ensure_token(invoice)])).content.decode()
        for html in (detail, public):
            self.assertIn("JNE", html)
            self.assertIn("JNE0001", html)
        from django.template.loader import render_to_string
        self.assertIn("Resi JNE0001", render_to_string("merchant/invoice_pdf.html", {"invoices": [invoice]}))
        self.assertIn("Resi JNE0001", render_to_string("tax/invoice.html", {"invoice": invoice}))

    def test_the_sale_shows_on_the_mitra_page_already_billed(self):
        self.post_invoice()
        page = self.client.get(reverse("merchant-abshop"))
        self.assertEqual((page.context["summary"]["total"], page.context["summary"]["unbilled"]), (1, 0))

    def test_editing_keeps_the_mitra(self):
        self.post_invoice()
        invoice = Invoice.objects.latest("created_at")
        self.client.post(reverse("merchant-invoice-edit", args=[invoice.pk]), {
            "buyer_name": "Mitra Jaya", "issued_on": self.today.isoformat(), "payment_status": "credit",
            "partner_id": str(self.mitra.pk), "product[]": [str(self.kabel.pk)], "description[]": [""],
            "qty[]": ["4"], "unit_price[]": ["70000"], "line_discount[]": [""], "unit[]": [""],
            "note[]": [""], "code[]": [""]})
        order = Invoice.objects.get(pk=invoice.pk).order
        self.assertEqual(order.consignment_partner_id, self.mitra.pk)
        self.assertEqual(order.abshop_billing_status, "billed")


class MitraBillingTests(Base):
    """Settlements and the mitra page read the mitra's own price list."""

    def test_item_price_follows_the_orders_mitra(self):
        from apps.orders.models import Order, OrderItem
        from apps.settlements.models import ConsignmentPartner, PartnerProductPrice
        a = ConsignmentPartner.objects.create(store=self.store, name="A")
        b = ConsignmentPartner.objects.create(store=self.store, name="B")
        PartnerProductPrice.objects.create(partner=a, product=self.kabel, price=Decimal(60_000))
        PartnerProductPrice.objects.create(partner=b, product=self.kabel, price=Decimal(80_000))
        self.kabel.ab_shop_price = Decimal(75_000)
        self.kabel.save()

        def item(partner):
            order = Order.objects.create(store=self.store, customer_name="m", phone="1", shipping_address="-",
                subtotal=1, total=1, channel="abshop", order_type="reseller_sell_through",
                consignment_partner=partner, idempotency_key=__import__("uuid").uuid4().hex)
            return OrderItem.objects.create(order=order, product=self.kabel, name="Kabel", sku="KBL",
                unit_price=100_000, quantity=1, line_total=100_000)
        self.assertEqual(item(a).effective_ab_shop_price, Decimal(60_000))
        self.assertEqual(item(b).effective_ab_shop_price, Decimal(80_000))
        self.assertEqual(item(None).effective_ab_shop_price, Decimal(75_000))  # legacy general price
        own = item(a)
        own.ab_shop_price = Decimal(55_000)
        self.assertEqual(own.effective_ab_shop_price, Decimal(55_000))         # a line price still wins

    def test_new_settlement_takes_only_this_mitras_unbilled_orders(self):
        from apps.orders.models import Order
        from apps.settlements.models import ConsignmentPartner
        a = ConsignmentPartner.objects.create(store=self.store, name="A")
        b = ConsignmentPartner.objects.create(store=self.store, name="B")

        def order(partner, status):
            return Order.objects.create(store=self.store, customer_name="m", phone="1", shipping_address="-",
                subtotal=1, total=1, channel="abshop", order_type="reseller_sell_through",
                consignment_partner=partner, abshop_billing_status=status,
                idempotency_key=f"ms-{partner.pk}-{status}")
        mine, theirs, billed = order(a, "unbilled"), order(b, "unbilled"), order(a, "billed")
        self.client.force_login(self.owner)
        self.client.post(reverse("settlement-new"), {"partner_id": a.pk,
            "period_start": (self.today.replace(day=1)).isoformat(), "period_end": self.today.isoformat()})
        from apps.settlements.models import SettlementLine
        taken = set(SettlementLine.objects.values_list("order_id", flat=True))
        self.assertEqual(taken, {mine.pk})
        self.assertNotIn(theirs.pk, taken)
        self.assertNotIn(billed.pk, taken)
