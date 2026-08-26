import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider, XenditPaymentProvider, doku_signature
from apps.integrations.shipping import RajaOngkirProvider
from apps.orders.models import (
    InventoryAllocation,
    Order,
    OrderItem,
    PaymentEvent,
    PaymentTransaction,
    ShippingMethod,
)
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship")
class ProviderIntegrationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("integration-owner")
        self.buyer = User.objects.create_user("integration-buyer")
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

    @override_settings(DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret",
        DOKU_API_URL="https://api-sandbox.doku.com", PUBLIC_BASE_URL="https://shop.example.com")
    @patch("apps.integrations.payments.request_json")
    def test_doku_creates_signed_checkout_session(self, request_json):
        request_json.return_value = {"response": {"payment": {
            "url": "https://sandbox.doku.com/checkout-link-v2/test"}}}
        order = Order.objects.create(store=self.store, customer_name="Ayu", email="ayu@example.com",
            phone="081234567890", shipping_address="Jakarta", subtotal=Decimal(200000),
            total=Decimal(218000), payment_provider="doku", idempotency_key="doku-test")

        session = DokuPaymentProvider().create_payment(order)

        call = request_json.call_args
        payload = json.loads(call.kwargs["encoded_payload"])
        headers = call.kwargs["headers"]
        self.assertEqual(session.reference, f"NK{order.id.hex[:26]}")
        self.assertEqual(payload["order"]["amount"], 218000)
        self.assertEqual(payload["order"]["currency"], "IDR")
        self.assertEqual(payload["callbacks"]["url"],
            f"https://shop.example.com/orders/{order.id}/?token={order.access_token}")
        self.assertTrue(headers["Signature"].startswith("HMACSHA256="))
        self.assertNotIn("doku-secret", json.dumps(payload))

    @override_settings(DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret")
    def test_doku_webhook_verifies_and_marks_paid_once(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(218000),
            payment_provider="doku", payment_reference="NKTEST", idempotency_key="doku-webhook")
        payload = {"order": {"invoice_number": "NKTEST", "amount": 218000,
            "currency": "IDR"}, "transaction": {"status": "SUCCESS",
            "original_request_id": "original-doku-request"}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = "2026-08-26T12:00:00Z"
        signature = doku_signature("MCH-TEST", "notification-1", timestamp,
            "/integrations/webhooks/doku/", body, "doku-secret")
        headers = {"HTTP_CLIENT_ID": "MCH-TEST", "HTTP_REQUEST_ID": "notification-1",
            "HTTP_REQUEST_TIMESTAMP": timestamp, "HTTP_SIGNATURE": signature}

        denied = Client().post("/integrations/webhooks/doku/", data=body,
            content_type="application/json", **{**headers, "HTTP_SIGNATURE": "wrong"})
        client = Client()
        accepted = client.post("/integrations/webhooks/doku/", data=body,
            content_type="application/json", **headers)
        duplicate = client.post("/integrations/webhooks/doku/", data=body,
            content_type="application/json", **headers)

        order.refresh_from_db()
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(order.status, "paid")
        self.assertTrue(PaymentTransaction.objects.get().reconciled)

    @override_settings(DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret")
    def test_doku_checkout_failed_notification_does_not_cancel_order(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(218000),
            payment_provider="doku", payment_reference="NKRETRY", idempotency_key="doku-retry")
        payload = {"order": {"invoice_number": "NKRETRY", "amount": 218000},
            "transaction": {"status": "FAILED", "original_request_id": "attempt-one"}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = "2026-08-26T12:00:00Z"
        signature = doku_signature("MCH-TEST", "failed-notification", timestamp,
            "/integrations/webhooks/doku/", body, "doku-secret")
        response = Client().post("/integrations/webhooks/doku/", data=body,
            content_type="application/json", HTTP_CLIENT_ID="MCH-TEST",
            HTTP_REQUEST_ID="failed-notification", HTTP_REQUEST_TIMESTAMP=timestamp,
            HTTP_SIGNATURE=signature)
        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ignored"])
        self.assertEqual(order.status, "pending")
        self.assertFalse(PaymentTransaction.objects.filter(order=order).exists())

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

    @override_settings(RAJAONGKIR_API_KEY="raja-test", RAJAONGKIR_ORIGIN_ID="31555")
    @patch("apps.integrations.shipping.request_form_json")
    def test_rajaongkir_tracking_normalizes_history(self, request_form_json):
        request_form_json.return_value = {"data": {
            "summary": {"status": "ON PROCESS"},
            "delivery_status": {"status": "ON PROCESS"},
            "manifest": [{"manifest_code": "TRANSIT",
                "manifest_description": "Paket tiba di gateway Jakarta",
                "manifest_date": "2026-08-27", "manifest_time": "08:00:00",
                "city_name": "Jakarta"}]}}
        result = RajaOngkirProvider().track("JNE123", "jne", "081234567890")
        url = request_form_json.call_args.args[0]
        self.assertIn("/track/waybill?", url)
        self.assertIn("awb=JNE123", url)
        self.assertIn("courier=jne", url)
        self.assertIn("last_phone_number=67890", url)
        self.assertEqual(result["status"], "ON PROCESS")
        self.assertEqual(result["events"][0]["location"], "Jakarta")

    @override_settings(SHIPPING_RATE_PROVIDER="rajaongkir", RAJAONGKIR_API_KEY="raja-test",
        RAJAONGKIR_ORIGIN_ID="31555")
    @patch("apps.integrations.shipping.request_form_json")
    def test_rajaongkir_checkout_identifies_live_provider(self, request_form_json):
        request_form_json.return_value = {"data": [{"name": "J&T Express", "code": "jnt",
            "service": "EZ", "cost": 55000, "etd": ""}]}
        client = Client()
        client.force_login(self.buyer)
        client.post(f"/cart/add/{self.product.id}/")
        response = client.post("/checkout/shipping-rates/", {"destination_area_id": "17620"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider_name"], "RajaOngkir")
        self.assertEqual(response.json()["quotes"][0]["price"], 55000)

    @override_settings(SHIPPING_RATE_PROVIDER="rajaongkir", RAJAONGKIR_API_KEY="raja-test",
        RAJAONGKIR_ORIGIN_POSTAL_CODE="10110", RAJAONGKIR_ORIGIN_ID="")
    @patch("apps.integrations.shipping.request_form_json")
    @patch("apps.integrations.shipping.request_json")
    def test_rajaongkir_resolves_origin_and_destination_postcodes(self, request_json,
            request_form_json):
        def destinations(url, **kwargs):
            if "10110" in url:
                return {"data": [{"id": 100, "label": "Gambir, Jakarta 10110",
                    "zip_code": "10110"}]}
            return {"data": [{"id": 200, "label": "Wonokromo, Surabaya 60242",
                "zip_code": "60242", "province_name": "Jawa Timur",
                "city_name": "Surabaya", "district_name": "Wonokromo"}]}
        request_json.side_effect = destinations
        request_form_json.return_value = {"data": [{"name": "JNE", "code": "jne",
            "service": "REG", "cost": 21000, "etd": "2-3 day"}]}
        client = Client()
        client.force_login(self.buyer)
        client.post(f"/cart/add/{self.product.id}/")

        response = client.post("/checkout/shipping-rates/", {"postal_code": "60242"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["destination_area_id"], "200")
        payload = request_form_json.call_args.args[1]
        self.assertEqual(payload["origin"], "100")
        self.assertEqual(payload["destination"], "200")

    @override_settings(SHIPPING_RATE_PROVIDER="rajaongkir", RAJAONGKIR_API_KEY="raja-test",
        RAJAONGKIR_ORIGIN_ID="31555")
    @patch("apps.integrations.shipping.request_form_json")
    @patch("apps.integrations.shipping.request_json")
    def test_rajaongkir_postcode_quote_can_complete_checkout(self, request_json,
            request_form_json):
        request_json.return_value = {"data": [{"id": 68424,
            "label": "Wonokromo, Surabaya 60243", "zip_code": "60243",
            "province_name": "Jawa Timur", "city_name": "Surabaya",
            "district_name": "Wonokromo"}]}
        request_form_json.return_value = {"data": [{"name": "JNE", "code": "jne",
            "service": "REG", "cost": 19000, "etd": "2-3 day"}]}
        client = Client()
        client.force_login(self.buyer)
        client.post(f"/cart/add/{self.product.id}/")
        quote_response = client.post("/checkout/shipping-rates/", {"postal_code": "60243"})
        token = quote_response.json()["quotes"][0]["token"]

        checkout = client.post("/checkout/", {"customer_name": "Ayu",
            "phone": "081234567890", "shipping_address": "Jl. Surabaya 1",
            "province": "Jawa Timur", "city": "Surabaya", "district": "Wonokromo",
            "postal_code": "60243", "shipping_method": token})

        order = Order.objects.get()
        self.assertEqual(checkout.status_code, 302)
        self.assertEqual(order.destination_area_id, "68424")
        self.assertEqual(order.courier_code, "jne")
        self.assertEqual(order.shipping_cost, 19000)

    @override_settings(BITESHIP_API_KEY="biteship_test.demo", BITESHIP_ORIGIN_POSTAL_CODE="10110")
    @patch("apps.integrations.shipping.request_json")
    def test_checkout_fetches_and_stores_biteship_quote(self, request_json):
        request_json.return_value = {"pricing": [{"courier_code": "jne", "courier_name": "JNE",
            "courier_service_code": "reg", "courier_service_name": "Reguler", "price": 23000,
            "duration": "2 - 3 days"}]}
        client = Client()
        client.force_login(self.buyer)
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

    @override_settings(XENDIT_WEBHOOK_VERIFICATION_TOKEN="webhook-secret")
    def test_failed_payment_releases_inventory_reservation(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(200000),
            payment_provider="xendit", payment_reference="ps-failed", idempotency_key="failed")
        item = OrderItem.objects.create(order=order, product=self.product, name=self.product.name,
            sku=self.product.sku, unit_price=self.product.price, quantity=1,
            line_total=self.product.price)
        level = InventoryLevel.objects.get(product=self.product)
        level.reserved = 1
        level.save(update_fields=["reserved"])
        InventoryAllocation.objects.create(order_item=item, inventory_level=level, quantity=1)
        payload = {"event": "payment_session.failed", "data": {"reference_id": str(order.id),
            "payment_session_id": "ps-failed"}}
        response = Client().post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="webhook-secret")
        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order.status, "payment_failed")
        self.assertEqual(PaymentTransaction.objects.get().status, "failed")
        level.refresh_from_db()
        self.assertEqual(level.reserved, 0)

    @override_settings(XENDIT_WEBHOOK_VERIFICATION_TOKEN="webhook-secret")
    def test_late_payment_after_cancellation_requires_reconciliation(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(200000), total=Decimal(200000),
            status="cancelled", payment_provider="xendit", payment_reference="ps-late",
            idempotency_key="late")
        payload = {"event": "payment_session.completed", "data": {"reference_id": str(order.id),
            "payment_session_id": "ps-late", "payment_request_id": "pr-late",
            "amount": 200000, "currency": "IDR"}}
        response = Client().post("/integrations/webhooks/xendit/", data=json.dumps(payload),
            content_type="application/json", HTTP_X_CALLBACK_TOKEN="webhook-secret")
        payment = PaymentTransaction.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, "succeeded")
        self.assertFalse(payment.reconciled)
        self.assertEqual(payment.provider_payment_id, "pr-late")
        self.assertIn("cancelled", payment.mismatch_reason)
