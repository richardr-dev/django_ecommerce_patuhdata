"""Tes koneksi DOKU: sellers and the platform can prove their keys work before buyers pay."""
import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, override_settings

from apps.integrations.http import ProviderError
from apps.integrations.models import StorePaymentSettings
from apps.integrations.payments import doku_signature
from apps.shops.models import AuditLog, Store

PAGE = {"response": {"payment": {"url": "https://sandbox.doku.test/pay/abc"}}}


@override_settings(DOKU_API_URL="https://api-sandbox.doku.com", DOKU_CLIENT_ID="MCH-PLATFORM",
    DOKU_SECRET_KEY="platform-secret", PAYMENT_PROVIDER="doku",
    PUBLIC_BASE_URL="https://lakuin.test")
class DokuConnectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Toko Ayu", slug="toko-ayu")
        self.account = StorePaymentSettings(store=self.store, gateway="doku",
            doku_client_id="MCH-SELLER")
        self.account.secret_key = "seller-secret"
        self.account.save()
        self.client = Client()
        self.client.force_login(self.owner)

    @patch("apps.integrations.payments.request_json", return_value=PAGE)
    def test_seller_test_opens_a_test_page_with_their_own_keys(self, request_json):
        reply = self.client.post("/merchant/payments/test/", follow=True)
        self.assertContains(reply, "Koneksi DOKU berhasil (Sandbox)")
        self.assertContains(reply, 'href="https://sandbox.doku.test/pay/abc"')

        sent = request_json.call_args
        body = json.loads(sent.kwargs["encoded_payload"])
        self.assertEqual(sent.kwargs["headers"]["Client-Id"], "MCH-SELLER")
        self.assertEqual(body["order"]["amount"], 10000)
        self.assertTrue(body["order"]["invoice_number"].startswith("LKTEST"))
        self.assertTrue(AuditLog.objects.filter(action="payment_settings.tested").exists())

    @patch("apps.integrations.payments.request_json",
        side_effect=ProviderError('Provider returned HTTP 401: {"error":"invalid signature"}'))
    def test_wrong_keys_are_explained(self, _request_json):
        reply = self.client.post("/merchant/payments/test/", follow=True)
        self.assertContains(reply, "DOKU menolak kredensial (Sandbox)")

    @patch("apps.integrations.payments.request_json",
        side_effect=ProviderError("Provider returned HTTP 503: <html>alb</html>"))
    def test_doku_downtime_is_explained_without_raw_html(self, _request_json):
        reply = self.client.post("/merchant/payments/test/", follow=True)
        self.assertContains(reply, "Server DOKU sedang tidak dapat dihubungi")
        self.assertNotContains(reply, "&lt;html&gt;")

    def test_only_the_store_admin_can_run_it_and_only_with_saved_keys(self):
        self.account.gateway = ""
        self.account.save()
        reply = self.client.post("/merchant/payments/test/", follow=True)
        self.assertContains(reply, "Simpan Client ID dan Secret Key DOKU terlebih dahulu")
        self.assertEqual(Client().post("/merchant/payments/test/").status_code, 302)

    def test_a_paid_test_page_is_acknowledged_without_touching_orders(self):
        path = "/integrations/webhooks/doku/"
        body = json.dumps({"order": {"invoice_number": "LKTESTabc", "amount": 10000},
            "transaction": {"status": "SUCCESS"}}, separators=(",", ":")).encode()
        stamp = "2026-11-04T10:00:00Z"
        reply = Client().post(path, data=body, content_type="application/json",
            HTTP_CLIENT_ID="MCH-PLATFORM", HTTP_REQUEST_ID="n1", HTTP_REQUEST_TIMESTAMP=stamp,
            HTTP_SIGNATURE=doku_signature("MCH-PLATFORM", "n1", stamp, path, body,
                "platform-secret"))
        self.assertEqual(reply.json(), {"received": True, "test": True})

    @patch("apps.integrations.payments.request_json", return_value=PAGE)
    def test_platform_check_command(self, request_json):
        out = StringIO()
        call_command("doku_check", stdout=out)
        self.assertIn("https://lakuin.test/integrations/webhooks/doku/", out.getvalue())
        self.assertIn("Kredensial diterima", out.getvalue())
        self.assertEqual(request_json.call_args.kwargs["headers"]["Client-Id"], "MCH-PLATFORM")

        request_json.side_effect = ProviderError("Provider returned HTTP 401: no")
        with self.assertRaisesMessage(CommandError, "DOKU menolak kredensial"):
            call_command("doku_check", stdout=StringIO())
