import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.http import ProviderError
from apps.integrations.payments import XenditPaymentProvider
from apps.integrations.shipping import RajaOngkirProvider
from apps.orders.models import Order, PaymentEvent, PaymentTransaction, ShippingMethod
from apps.shops.models import Store


class ProviderIntegrationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("integration-owner")
        self.store = Store.objects.create(owner=owner, name="Integration Shop", slug="integration-shop")
        self.product = Product.objects.create(store=self.store, name="Sepatu", slug="sepatu", sku="SP-1",
            price=200000, weight_grams=500, length_cm=30, width_cm=20, height_cm=12)
        warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)

    @override_settings(XENDIT_SECRET_KEY="xnd_development_test", PUBLIC_BASE_URL="https://shop.example.com")
    @patch("apps.integrations.payments.request_json")
    def test_xendit_creates_server_side_payment_session(self, request_json):
        request_json.return_value = {"payment_session_id": "ps-test", "payment_link_url": "https://dev.xen.to/test",
            "expires_at": "2026-08-26T12:00:00Z"}
        order = Order.objects.create(store=self.store, customer_name="Ayu Putri", email="ayu@example.com",
            phone="081234567890", shipping_address="Jakarta", subtotal=Decimal(200000),
            total=Decimal(218000), payment_provider="xendit", idempotency_key="xendit-test")
        session = XenditPaymentProvider().create_payment(order)
        payload = request_json.call_args.kwargs["payload"]
        self.assertEqual(session.reference, "ps-test")
        self.assertEqual(payload["amount"], 218000)
        self.assertEqual(payload["currency"], "IDR")
        self.assertEqual(payload["reference_id"], str(order.id))
        self.assertNotIn("XENDIT_SECRET_KEY", json.dumps(payload))

    @override_settings(XENDIT_SECRET_KEY="xnd_development_test", PUBLIC_BASE_URL="http://localhost:8000")
    @patch("apps.integrations.payments.request_json")
    def test_xendit_local_session_omits_https_return_urls(self, request_json):
        request_json.return_value = {"payment_session_id": "ps-local", "payment_link_url": "https://dev.xen.to/local"}
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(200000),
            payment_provider="xendit", idempotency_key="xendit-local")
        session = XenditPaymentProvider().create_payment(order)
        payload = request_json.call_args.kwargs["payload"]
        self.assertEqual(session.reference, "ps-local")
        self.assertNotIn("success_return_url", payload)
        self.assertNotIn("cancel_return_url", payload)

    @override_settings(BITESHIP_API_KEY="biteship_test.demo", BITESHIP_ORIGIN_POSTAL_CODE="10110")
    @patch("apps.integrations.shipping.request_json")
    def test_biteship_address_search(self, request_json):
        request_json.return_value = {"areas": [{"id": "ID-AREA-1",
            "name": "Menteng, Jakarta Pusat, DKI Jakarta. 10310", "postal_code": 10310,
            "administrative_division_level_1_name": "DKI Jakarta",
            "administrative_division_level_2_name": "Jakarta Pusat",
            "administrative_division_level_3_name": "Menteng"}]}
        response = Client().get("/checkout/shipping-areas/", {"q": "Menteng"})
        area = response.json()["areas"][0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(area["id"], "ID-AREA-1")
        self.assertEqual(area["postal_code"], "10310")
        self.assertEqual(area["province"], "DKI Jakarta")

    @override_settings(RAJAONGKIR_API_KEY="raja-test", RAJAONGKIR_ORIGIN_ID="31555")
    @patch("apps.integrations.shipping.request_json")
    def test_rajaongkir_address_search(self, request_json):
        request_json.return_value = {"data": [{"id": 68423, "label": "Wonokromo, Surabaya 60241",
            "province_name": "Jawa Timur", "city_name": "Surabaya", "district_name": "Wonokromo",
            "subdistrict_name": "Wonokromo", "zip_code": "60241"}]}
        area = RajaOngkirProvider().search_areas("Wonokromo")[0]
        self.assertEqual(area["id"], "68423")
        self.assertEqual(area["postal_code"], "60241")

    @override_settings(RAJAONGKIR_API_KEY="raja-test", RAJAONGKIR_ORIGIN_ID="31555",
        RAJAONGKIR_COURIERS="jne:jnt")
    @patch("apps.integrations.shipping.request_form_json")
    def test_rajaongkir_real_rate_mapping(self, request_form_json):
        request_form_json.return_value = {"data": [{"name": "Jalur Nugraha Ekakurir", "code": "jne",
            "service": "REG", "cost": 19000, "etd": "2-3 day"}]}
        quote = RajaOngkirProvider().rates("68423", [{"product": self.product, "quantity": 2}])[0]
        payload = request_form_json.call_args.args[1]
        self.assertEqual(quote.price, 19000)
        self.assertEqual(quote.provider, "rajaongkir")
        self.assertEqual(payload["weight"], 1000)
        self.assertEqual(payload["destination"], "68423")

    @override_settings(SHIPPING_RATE_PROVIDER="rajaongkir", RAJAONGKIR_API_KEY="raja-test",
        RAJAONGKIR_ORIGIN_ID="31555")
    @patch("apps.integrations.shipping.request_form_json")
    def test_rajaongkir_checkout_identifies_live_provider(self, request_form_json):
        request_form_json.return_value = {"data": [{"name": "J&T Express", "code": "jnt",
            "service": "EZ", "cost": 55000, "etd": ""}]}
        client = Client()
        client.post(f"/cart/add/{self.product.id}/")
        response = client.post("/checkout/shipping-rates/", {"destination_area_id": "17620"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider_name"], "RajaOngkir")
        self.assertEqual(response.json()["quotes"][0]["price"], 55000)

    @override_settings(BITESHIP_API_KEY="biteship_test.demo", BITESHIP_ORIGIN_POSTAL_CODE="10110")
    @patch("apps.integrations.shipping.request_json")
    def test_checkout_fetches_and_stores_biteship_quote(self, request_json):
        request_json.return_value = {"pricing": [{"courier_code": "jne", "courier_name": "JNE",
            "courier_service_code": "reg", "courier_service_name": "Reguler", "price": 23000,
            "duration": "2 - 3 days"}]}
        client = Client()
        client.post(f"/cart/add/{self.product.id}/")
        response = client.post("/checkout/shipping-rates/", {"postal_code": "60241"})
        quote = response.json()["quotes"][0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(quote["price"], 23000)
        self.assertIn(quote["token"], client.session["shipping_quotes"])
        self.assertEqual(request_json.call_args.kwargs["payload"]["items"][0]["weight"], 500)
        checkout = client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jl. Surabaya 1", "province": "Jawa Timur", "city": "Surabaya",
            "district": "Wonokromo", "postal_code": "60241", "shipping_method": quote["token"]})
        order = Order.objects.get()
        self.assertEqual(checkout.status_code, 302)
        self.assertEqual(order.shipping_cost, 23000)
        self.assertEqual(order.shipping_provider, "biteship")

    @override_settings(BITESHIP_API_KEY="biteship_test.demo", BITESHIP_ORIGIN_POSTAL_CODE="10110")
    @patch("apps.integrations.shipping.request_json")
    def test_biteship_insufficient_balance_uses_labeled_demo_rate(self, request_json):
        request_json.side_effect = ProviderError("Provider returned HTTP 400: No sufficient balance to call rates API")
        ShippingMethod.objects.create(store=self.store, code="demo-reg", name="Reguler", flat_rate=18000)
        client = Client()
        client.post(f"/cart/add/{self.product.id}/")
        response = client.post("/checkout/shipping-rates/", {"postal_code": "60241"})
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["fallback"])
        self.assertEqual(data["quotes"][0]["price"], 18000)
        self.assertIn("tarif demo", data["quotes"][0]["name"])

    @override_settings(XENDIT_WEBHOOK_VERIFICATION_TOKEN="webhook-secret")
    def test_xendit_webhook_verifies_and_marks_paid_once(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", email="ayu@example.com",
            phone="081234567890", shipping_address="Jakarta", subtotal=Decimal(200000),
            total=Decimal(218000), payment_provider="xendit", payment_reference="ps-test",
            idempotency_key="webhook-test")
        payload = {"event": "payment_session.completed", "created": "2026-08-26T12:00:00Z",
            "data": {"reference_id": str(order.id), "payment_session_id": "ps-test",
                "amount": 218000, "currency": "IDR"}}
        client = Client()
        denied = client.post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="wrong")
        accepted = client.post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="webhook-secret")
        duplicate = client.post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="webhook-secret")
        order.refresh_from_db()
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(order.status, "paid")
        self.assertEqual(PaymentEvent.objects.count(), 1)
        transaction = PaymentTransaction.objects.get()
        self.assertTrue(transaction.reconciled)
        self.assertEqual(transaction.status, "succeeded")

    @override_settings(XENDIT_WEBHOOK_VERIFICATION_TOKEN="webhook-secret")
    def test_xendit_rejects_and_records_payment_amount_mismatch(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(200000),
            payment_provider="xendit", payment_reference="ps-mismatch", idempotency_key="mismatch")
        payload = {"event": "payment_session.completed", "data": {"reference_id": str(order.id),
            "payment_session_id": "ps-mismatch", "amount": 100000, "currency": "IDR"}}
        response = Client().post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="webhook-secret")
        order.refresh_from_db()
        payment = PaymentTransaction.objects.get()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(order.status, "pending")
        self.assertFalse(payment.reconciled)
        self.assertEqual(payment.status, "failed")
