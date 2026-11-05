"""Notifikasi WhatsApp from the seller's own sender (Fonnte or Meta Cloud API)."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from apps.catalog.models import Product
from apps.integrations.http import ProviderError
from apps.integrations.models import OutboxEvent, StoreWhatsAppSettings, WhatsAppMessage
from apps.integrations.whatsapp import CloudApiSender, msisdn
from apps.orders.models import Order, OrderItem
from apps.orders.services import transition_order
from apps.shops.models import AuditLog, Store, StoreStaff

FONNTE = "apps.integrations.whatsapp.request_form_json"


@override_settings(PLATFORM_DOMAIN="", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WhatsAppNotificationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("wa-owner")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.account = StoreWhatsAppSettings(store=self.store, provider="fonnte")
        self.account.token = "device-token"
        self.account.save()
        self.order = Order.objects.create(store=self.store, customer_name="Sri",
            phone="0812-3456-7890", email="sri@example.com", shipping_address="Jakarta",
            subtotal=Decimal(150000), total=Decimal(150000), idempotency_key="wa-1")
        product = Product.objects.create(store=self.store, name="Kemeja", slug="kemeja",
            price=Decimal(150000))
        OrderItem.objects.create(order=self.order, product=product, name="Kemeja", sku="K1",
            unit_price=Decimal(150000), quantity=1, line_total=Decimal(150000))

    def _event(self, topic, **payload):
        return OutboxEvent.objects.create(store=self.store, topic=topic,
            aggregate_id=str(self.order.id), payload={"order_id": str(self.order.id), **payload})

    def test_phone_numbers_become_whatsapp_ids(self):
        self.assertEqual(msisdn("0812-3456-7890"), "6281234567890")
        self.assertEqual(msisdn("+62 812 3456 7890"), "6281234567890")
        self.assertEqual(msisdn("812345678"), "62812345678")
        self.assertEqual(msisdn("0212345678"), "")  # landline
        self.assertEqual(msisdn("-"), "")

    @patch(FONNTE, return_value={"status": True, "id": ["111"]})
    def test_new_order_sends_payment_link_once(self, post):
        self._event("order.created")
        call_command("process_outbox")
        call_command("process_outbox")

        post.assert_called_once()
        url, data = post.call_args.args
        self.assertEqual(url, "https://api.fonnte.com/send")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "device-token"})
        self.assertEqual(post.call_args.kwargs["attempts"], 1)
        self.assertEqual(data["target"], "6281234567890")
        self.assertIn("1× Kemeja", data["message"])
        self.assertIn("Rp150.000", data["message"])
        self.assertIn(str(self.order.access_token), data["message"])
        message = WhatsAppMessage.objects.get()
        self.assertEqual((message.kind, message.status, message.provider_message_id),
            ("order_created", "sent", "111"))

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_payment_alerts_buyer_and_seller(self, post):
        self.account.seller_alert_phone = "081999888777"
        self.account.save()
        self._event("payment.received")
        call_command("process_outbox")

        targets = [call.args[1]["target"] for call in post.call_args_list]
        self.assertEqual(targets, ["6281234567890", "6281999888777"])
        self.assertIn("Pesanan baru dibayar", post.call_args_list[1].args[1]["message"])

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_shipping_message_has_courier_and_waybill(self, post):
        self.order.shipping_service, self.order.tracking_number = "JNE REG", "JNE123"
        self.order.save()
        self._event("order.status_changed", status="shipped", **{"from": "packed"})
        call_command("process_outbox")

        text = post.call_args.args[1]["message"]
        self.assertIn("Kurir: JNE REG", text)
        self.assertIn("No. resi: JNE123", text)

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_switched_off_updates_and_paid_status_are_not_sent(self, post):
        self.account.notify_delivered = False
        self.account.save()
        self._event("order.status_changed", status="delivered", **{"from": "shipped"})
        self._event("order.status_changed", status="paid", **{"from": "pending"})
        call_command("process_outbox")
        post.assert_not_called()

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_abandoned_checkout_cancellation_is_silent(self, post):
        self._event("order.status_changed", status="cancelled", **{"from": "pending"})
        call_command("process_outbox")
        post.assert_not_called()

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_marketplace_orders_are_skipped(self, post):
        from apps.channels.models import ChannelOrder, SalesChannel

        channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Ruuma")
        ChannelOrder.objects.create(channel=channel, order=self.order, external_order_id="S1")
        self._event("payment.received")
        call_command("process_outbox")
        post.assert_not_called()

    def test_not_connected_store_sends_nothing(self):
        self.account.provider = ""
        self.account.save()
        with patch(FONNTE) as post:
            self._event("order.created")
            call_command("process_outbox")
        post.assert_not_called()

    def test_whatsapp_outage_retries_without_emailing_again(self):
        event = self._event("order.created")
        with patch(FONNTE, side_effect=ProviderError("Provider is temporarily unavailable")):
            call_command("process_outbox")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(len(mail.outbox), 1)

        OutboxEvent.objects.filter(pk=event.pk).update(next_retry_at=None)
        with patch(FONNTE, return_value={"status": True, "id": ["9"]}) as post:
            call_command("process_outbox")
        event.refresh_from_db()
        self.assertEqual(event.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)
        post.assert_called_once()
        self.assertEqual(list(WhatsAppMessage.objects.order_by("created_at")
            .values_list("status", flat=True)), ["failed", "sent"])

    @patch(FONNTE, return_value={"status": False, "reason": "invalid target"})
    def test_rejected_number_is_logged_not_retried(self, post):
        event = self._event("order.created")
        call_command("process_outbox")
        event.refresh_from_db()
        self.assertEqual(event.status, "succeeded")
        message = WhatsAppMessage.objects.get()
        self.assertEqual((message.status, message.error), ("failed", "invalid target"))

    @patch("apps.integrations.whatsapp.request_json", return_value={"messages": [{"id": "wamid.1"}]})
    def test_cloud_api_sends_the_approved_template(self, post):
        self.account.provider, self.account.phone_number_id = "cloud_api", "10987"
        self.account.template_name = "lakuin_update"
        self.account.save()
        message_id = CloudApiSender(self.account).send("6281234567890", "Halo\n  Sri\n\nTotal")

        self.assertEqual(message_id, "wamid.1")
        self.assertEqual(post.call_args.args[0], "https://graph.facebook.com/v21.0/10987/messages")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer device-token"})
        template = post.call_args.kwargs["payload"]["template"]
        self.assertEqual(template["name"], "lakuin_update")
        self.assertEqual(template["components"][0]["parameters"][0]["text"], "Halo · Sri · Total")

    @patch(FONNTE, return_value={"status": True, "id": ["1"]})
    def test_real_status_change_reaches_the_buyer(self, post):
        for status in ("paid", "confirmed", "allocated", "picking", "packed"):
            transition_order(self.order, status)
        self.order.tracking_number = "JNT77"
        self.order.save(update_fields=["tracking_number"])
        transition_order(self.order, "shipped")
        call_command("process_outbox")
        kinds = list(WhatsAppMessage.objects.values_list("kind", flat=True))
        self.assertEqual(kinds, ["shipped"])


@override_settings(PLATFORM_DOMAIN="")
class WhatsAppSettingsPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("wa-seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma",
            support_phone="081234567890")
        self.client = Client()
        self.client.force_login(self.owner)

    def _save(self, **data):
        base = {"provider": "fonnte", "token": "", "template_language": "id",
            "notify_order_created": "on", "notify_shipped": "on"}
        return self.client.post("/merchant/whatsapp/", {**base, **data})

    def test_token_is_encrypted_and_kept_when_left_blank(self):
        self._save(token="secret-device-token")
        account = StoreWhatsAppSettings.objects.get(store=self.store)
        self.assertNotIn("secret-device-token", account.encrypted_token)
        self.assertTrue(account.ready)
        self.assertFalse(account.notify_payment_received)

        self._save(seller_alert_phone="0819 9988 8777")
        account.refresh_from_db()
        self.assertEqual(account.token, "secret-device-token")
        self.assertEqual(account.seller_alert_phone, "0819 9988 8777")
        self.assertTrue(AuditLog.objects.filter(store=self.store,
            action="whatsapp.settings_updated").exists())
        page = self.client.get("/merchant/whatsapp/")
        self.assertContains(page, "Fonnte terhubung")
        self.assertNotContains(page, "secret-device-token")

    def test_cloud_api_needs_sender_and_template(self):
        response = self._save(provider="cloud_api", token="meta-token")
        self.assertContains(response, "Wajib untuk WhatsApp Cloud API.", count=2)
        self.assertFalse(StoreWhatsAppSettings.objects.get(store=self.store).provider)

    def test_switching_provider_asks_for_a_new_token(self):
        self._save(token="fonnte-token")
        response = self._save(provider="cloud_api", phone_number_id="1", template_name="t")
        self.assertContains(response, "Masukkan token")

    @patch(FONNTE, return_value={"status": True, "id": ["5"]})
    def test_test_message(self, post):
        self._save(token="device-token")
        response = self.client.post("/merchant/whatsapp/test/", {"phone": "081234567890"},
            follow=True)
        self.assertContains(response, "Pesan tes terkirim ke +6281234567890")
        self.assertEqual(WhatsAppMessage.objects.get().kind, "test")
        self.assertContains(response, "Tes")

    def test_only_admins_manage_whatsapp(self):
        staff = User.objects.create_user("wa-ops")
        StoreStaff.objects.create(store=self.store, user=staff, role="ops")
        client = Client()
        client.force_login(staff)
        self.assertIn(client.get("/merchant/whatsapp/").status_code, {403, 404})
