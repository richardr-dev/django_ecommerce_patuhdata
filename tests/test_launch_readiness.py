from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider, PaymentSession, doku_signature
from apps.orders.models import (
    Coupon,
    Order,
    OrderItem,
    PaymentTransaction,
    ShipmentTrackingEvent,
)
from apps.orders.services import expire_pending_orders, transition_order
from apps.orders.shipments import ShipmentBookingError, book_shipment
from apps.orders.tracking import sync_order_tracking
from apps.shops.models import AuditLog, Store

CHECKOUT_FORM = {"customer_name": "Ayu", "email": "ayu@example.com", "phone": "081234567890",
    "shipping_address": "Jl. Merdeka 1, Jakarta"}


def _paid_flow(order, *statuses):
    for status in statuses:
        transition_order(order, status)


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PaymentSessionOutsideLockTests(TransactionTestCase):
    """The gateway call must run after the stock/coupon transaction has committed."""

    def setUp(self):
        owner = User.objects.create_user("lock-owner")
        self.store = Store.objects.create(owner=owner, name="Lock Shop", slug="lock-shop")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI-1", price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=3)
        self.coupon = Coupon.objects.create(store=self.store, code="HEMAT", kind="fixed", value=5000,
            usage_limit=5)
        self.client = Client()
        self.client.force_login(User.objects.create_user("lock-buyer"))
        self.client.post(f"/cart/add/{self.product.id}/")

    def test_gateway_is_called_without_an_open_transaction(self):
        seen = {}

        def create_payment(provider, order):
            seen["in_atomic_block"] = connection.in_atomic_block
            seen["reserved"] = InventoryLevel.objects.get(id=self.level.id).reserved
            seen["reference"] = Order.objects.get(id=order.id).payment_reference
            return PaymentSession(reference=f"demo-{order.id}", checkout_url="/pay/")

        with patch("apps.integrations.payments.DemoPaymentProvider.create_payment", create_payment):
            response = self.client.post("/checkout/", {**CHECKOUT_FORM, "coupon_code": "HEMAT"})

        order = Order.objects.get()
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)
        self.assertFalse(seen["in_atomic_block"])
        self.assertEqual(seen["reserved"], 1)
        self.assertEqual(seen["reference"], f"demo-{order.id}")
        self.assertEqual(order.payment_checkout_url, "/pay/")
        self.assertTrue(PaymentTransaction.objects.filter(order=order, status="pending").exists())

    def test_gateway_failure_cancels_order_and_releases_everything(self):
        with patch("apps.integrations.payments.DemoPaymentProvider.create_payment",
                side_effect=ProviderError("Provider returned HTTP 500: down")):
            response = self.client.post("/checkout/", {**CHECKOUT_FORM, "coupon_code": "HEMAT"})

        self.assertRedirects(response, "/checkout/", fetch_redirect_response=False)
        order = Order.objects.get()
        self.level.refresh_from_db()
        self.coupon.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.level.reserved, 0)
        self.assertEqual(self.coupon.used_count, 0)
        self.assertFalse(PaymentTransaction.objects.exists())
        self.assertEqual(self.client.session["cart"], {f"p:{self.product.id}": 1})
        self.assertNotEqual(self.client.session["checkout_key"], order.idempotency_key)

        call_command("process_outbox")
        self.assertEqual(len(mail.outbox), 0)

        # The customer can retry immediately with the cart that was kept.
        response = self.client.post("/checkout/", {**CHECKOUT_FORM, "coupon_code": "HEMAT"})
        self.assertEqual(Order.objects.filter(status="pending").count(), 1)


@override_settings(DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret",
    DOKU_API_URL="https://api-sandbox.doku.com", DOKU_PAYMENT_DUE_MINUTES=60)
class DokuLifecycleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("doku-owner")
        self.store = Store.objects.create(owner=self.owner, name="Doku Shop", slug="doku-shop")
        self.product = Product.objects.create(store=self.store, name="Teh", slug="teh", sku="TEH-1",
            price=100000)
        warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.product,
            on_hand=2, reserved=1)
        self.order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jakarta", subtotal=Decimal(100000), total=Decimal(100000),
            payment_provider="doku", idempotency_key="doku-life")
        self.order.payment_reference = DokuPaymentProvider.reference_for(self.order)
        self.order.payment_expires_at = timezone.now() - timedelta(minutes=1)
        self.order.save(update_fields=["payment_reference", "payment_expires_at"])
        item = OrderItem.objects.create(order=self.order, product=self.product, name="Teh",
            sku="TEH-1", unit_price=100000, quantity=1, line_total=100000)
        item.allocations.create(inventory_level=self.level, quantity=1)

    @patch("apps.integrations.payments.request_json")
    def test_checkout_session_carries_expiry_for_stock_release(self, request_json):
        request_json.return_value = {"response": {"payment": {"url": "https://sandbox.doku.com/p"}}}
        before = datetime.now(UTC)
        session = DokuPaymentProvider().create_payment(self.order)
        expires_at = datetime.fromisoformat(session.expires_at)
        self.assertGreaterEqual(expires_at, before + timedelta(minutes=65))
        self.assertLess(expires_at, before + timedelta(minutes=66))

    @patch("apps.integrations.payments.request_json")
    def test_status_check_is_signed_without_digest(self, request_json):
        request_json.return_value = {"transaction": {"status": "PENDING"}}
        DokuPaymentProvider().fetch_status(self.order)
        url = request_json.call_args.args[0]
        headers = request_json.call_args.kwargs["headers"]
        target = f"/orders/v1/status/{self.order.payment_reference}"
        self.assertEqual(url, f"https://api-sandbox.doku.com{target}")
        expected = doku_signature("MCH-TEST", headers["Request-Id"], headers["Request-Timestamp"],
            target, None, "doku-secret")
        self.assertEqual(headers["Signature"], expected)

    @patch("apps.integrations.payments.request_json")
    def test_expired_order_paid_at_doku_is_marked_paid_not_cancelled(self, request_json):
        request_json.return_value = {"order": {"invoice_number": self.order.payment_reference,
            "amount": 100000}, "transaction": {"status": "SUCCESS",
                "original_request_id": "doku-original"}}
        self.assertEqual(expire_pending_orders(), 0)
        self.order.refresh_from_db()
        self.level.refresh_from_db()
        self.assertEqual(self.order.status, "paid")
        self.assertEqual(self.level.reserved, 1)
        payment = PaymentTransaction.objects.get(order=self.order)
        self.assertEqual((payment.external_id, payment.status, payment.reconciled),
            ("doku-original", "succeeded", True))

    @patch("apps.integrations.payments.request_json")
    def test_expired_order_without_payment_attempt_is_cancelled(self, request_json):
        request_json.side_effect = ProviderError("Provider returned HTTP 404: not found")
        self.assertEqual(expire_pending_orders(), 1)
        self.order.refresh_from_db()
        self.level.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(self.level.reserved, 0)

    @patch("apps.integrations.payments.request_json")
    def test_pending_doku_payment_keeps_reservation(self, request_json):
        request_json.return_value = {"transaction": {"status": "PENDING"}}
        self.assertEqual(expire_pending_orders(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")

    @patch("apps.integrations.payments.request_json")
    def test_unreachable_gateway_retries_then_cancels_after_grace(self, request_json):
        request_json.side_effect = ProviderError("Provider is temporarily unavailable after retry.")
        self.assertEqual(expire_pending_orders(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")

        self.assertEqual(expire_pending_orders(now=timezone.now() + timedelta(hours=7)), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")

    def test_manual_transfer_refund_completes_doku_refund(self):
        PaymentTransaction.objects.create(order=self.order, provider="doku", external_id="pay-1",
            status="succeeded", reconciled=True, amount=Decimal(100000))
        _paid_flow(self.order, "paid")
        client = Client()
        client.force_login(self.owner)

        missing = client.post(f"/merchant/orders/{self.order.id}/refund/",
            {"amount": "100000", "method": "manual_transfer"})
        self.assertFalse(self.order.refunds.exists())
        self.assertEqual(missing.status_code, 302)

        client.post(f"/merchant/orders/{self.order.id}/refund/", {"amount": "100000",
            "method": "manual_transfer", "transfer_reference": "BCA-TRF-7781", "note": "Batal"})
        self.order.refresh_from_db()
        refund = self.order.refunds.get()
        self.assertEqual(self.order.status, "refunded")
        self.assertEqual((refund.status, refund.method), ("succeeded", "manual_transfer"))
        self.assertEqual(refund.payload["transfer_reference"], "BCA-TRF-7781")
        self.assertTrue(PaymentTransaction.objects.filter(order=self.order, kind="refund",
            status="succeeded").exists())


class SkuUniquenessTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("sku-owner")
        self.store = Store.objects.create(owner=owner, name="SKU Shop", slug="sku-shop")
        self.shirt = Product.objects.create(store=self.store, name="Kaos", slug="kaos", sku="KAOS",
            price=1)
        self.hat = Product.objects.create(store=self.store, name="Topi", slug="topi", sku="TOPI",
            price=1)
        ProductVariant.objects.create(product=self.shirt, name="M", sku="KAOS-M")
        self.client = Client()
        self.client.force_login(owner)

    def test_variant_cannot_reuse_a_product_sku(self):
        with self.assertRaises(ValidationError):
            ProductVariant.objects.create(product=self.shirt, name="L", sku="TOPI")

    def test_variant_sku_is_unique_across_products_in_store(self):
        with self.assertRaises(ValidationError):
            ProductVariant.objects.create(product=self.hat, name="M", sku="KAOS-M")

    def test_product_cannot_reuse_a_variant_sku(self):
        with self.assertRaises(ValidationError):
            Product.objects.create(store=self.store, name="Lain", slug="lain", sku="KAOS-M", price=1)

    def test_other_store_may_use_the_same_sku(self):
        other = Store.objects.create(owner=self.store.owner, name="Other", slug="other-sku")
        product = Product.objects.create(store=other, name="Kaos", slug="kaos", sku="KAOS-M", price=1)
        ProductVariant.objects.create(product=product, name="L", sku="TOPI")

    def test_renaming_a_variant_keeps_its_own_sku(self):
        variant = ProductVariant.objects.get(sku="KAOS-M")
        variant.name = "Medium"
        variant.save()

    def test_back_office_form_shows_collision_as_field_error(self):
        response = self.client.post("/merchant/setup/products/new/", {"name": "Baru", "slug": "baru",
            "sku": "KAOS-M", "price": "1000", "weight_grams": 0, "length_cm": 0, "width_cm": 0,
            "height_cm": 0})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SKU KAOS-M sudah dipakai")
        self.assertFalse(Product.objects.filter(slug="baru").exists())


@override_settings(BITESHIP_API_KEY="biteship_test.key", BITESHIP_ORIGIN_POSTAL_CODE="12950",
    BITESHIP_ORIGIN_CONTACT_NAME="", BITESHIP_ORIGIN_CONTACT_PHONE="", BITESHIP_ORIGIN_ADDRESS="")
class BiteshipBookingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ship-owner")
        self.store = Store.objects.create(owner=self.owner, name="Ship Shop", slug="ship-shop",
            support_phone="081200000000")
        Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT",
            address="Jl. Gudang 5, Jakarta Selatan")
        product = Product.objects.create(store=self.store, name="Sepatu", slug="sepatu", sku="SP-1",
            price=200000, weight_grams=800, length_cm=30, width_cm=20, height_cm=12)
        self.order = Order.objects.create(store=self.store, customer_name="Ayu", phone="081234567890",
            shipping_address="Jl. Merdeka 1", postal_code="10310", subtotal=Decimal(200000),
            total=Decimal(218000), shipping_provider="biteship", courier_code="jne",
            courier_service_code="reg", idempotency_key="ship-order")
        OrderItem.objects.create(order=self.order, product=product, name="Sepatu", sku="SP-1",
            unit_price=200000, quantity=1, line_total=200000)
        _paid_flow(self.order, "paid", "confirmed", "allocated", "picking", "packed")
        self.client = Client()
        self.client.force_login(self.owner)

    @patch("apps.integrations.shipping.request_json")
    def test_merchant_books_checkout_courier_and_waybill_is_saved(self, request_json):
        request_json.return_value = {"success": True, "id": "bs-order-1", "status": "confirmed",
            "price": 18000, "courier": {"waybill_id": "JNE123", "tracking_id": "trk-1"}}
        response = self.client.post(f"/merchant/orders/{self.order.id}/book-shipment/")
        self.assertRedirects(response, "/merchant/orders/", fetch_redirect_response=False)

        payload = request_json.call_args.kwargs["payload"]
        self.assertEqual(request_json.call_args.kwargs["attempts"], 1)
        self.assertEqual((payload["courier_company"], payload["courier_type"]), ("jne", "reg"))
        self.assertEqual(payload["origin_address"], "Jl. Gudang 5, Jakarta Selatan")
        self.assertEqual(payload["origin_contact_phone"], "081200000000")
        self.assertEqual(payload["destination_postal_code"], 10310)
        self.assertEqual(payload["items"][0]["weight"], 800)

        self.order.refresh_from_db()
        self.assertEqual(self.order.external_shipping_id, "bs-order-1")
        self.assertEqual(self.order.tracking_number, "JNE123")
        self.assertEqual(self.order.tracking_status, "awaiting_pickup")
        self.assertTrue(AuditLog.objects.filter(action="order.shipment_booked").exists())

        with self.assertRaises(ShipmentBookingError):
            book_shipment(self.order, actor=self.owner)
        self.assertEqual(request_json.call_count, 1)

    @patch("apps.integrations.shipping.request_json")
    def test_rejected_booking_can_be_retried(self, request_json):
        request_json.side_effect = ProviderError("Provider returned HTTP 400: invalid courier")
        with self.assertRaises(ProviderError):
            book_shipment(self.order, actor=self.owner)
        self.order.refresh_from_db()
        self.assertEqual(self.order.external_shipping_id, "")
        self.assertIn("invalid courier", self.order.tracking_error)

    @patch("apps.integrations.shipping.request_json")
    def test_uncertain_booking_is_not_resent(self, request_json):
        request_json.side_effect = ProviderError("Provider is temporarily unavailable after retry.")
        with self.assertRaises(ProviderError):
            book_shipment(self.order, actor=self.owner)
        self.order.refresh_from_db()
        self.assertTrue(self.order.external_shipping_id.startswith("booking:"))
        with self.assertRaises(ShipmentBookingError):
            book_shipment(self.order, actor=self.owner)
        page = self.client.get("/merchant/orders/")
        self.assertContains(page, "Cek dashboard Biteship")

    def test_booking_requires_packed_status(self):
        order = Order.objects.create(store=self.store, customer_name="Budi", phone="0812",
            shipping_address="Bandung", subtotal=1, total=1, shipping_provider="biteship",
            courier_code="jne", courier_service_code="reg", idempotency_key="not-packed")
        with self.assertRaises(ShipmentBookingError):
            book_shipment(order, actor=self.owner)

    def test_booking_requires_origin_details(self):
        self.store.support_phone = ""
        self.store.save(update_fields=["support_phone"])
        with self.assertRaisesMessage(ShipmentBookingError, "nomor telepon toko"):
            book_shipment(self.order, actor=self.owner)

    @patch("apps.integrations.shipping.request_json")
    def test_booked_shipment_tracking_moves_order_to_shipped_then_delivered(self, request_json):
        Order.objects.filter(id=self.order.id).update(external_shipping_id="bs-order-1")
        self.order.refresh_from_db()
        request_json.return_value = {"id": "bs-order-1", "status": "picked", "courier": {
            "waybill_id": "JNE999", "history": [
                {"note": "Kurir menuju lokasi", "status": "allocated", "updated_at": "2026-10-15T08:00"},
                {"note": "Paket diambil kurir", "status": "picked", "updated_at": "2026-10-15T10:00"}]}}
        sync_order_tracking(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "shipped")
        self.assertEqual(self.order.tracking_status, "in_transit")
        self.assertEqual(self.order.tracking_number, "JNE999")
        self.assertEqual(ShipmentTrackingEvent.objects.filter(order=self.order,
            provider="biteship").count(), 2)

        request_json.return_value = {"id": "bs-order-1", "status": "delivered",
            "courier": {"waybill_id": "JNE999", "history": []}}
        sync_order_tracking(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "delivered")


class AnonymousCartTests(TestCase):
    def test_cart_renders_for_logged_out_visitor(self):
        owner = User.objects.create_user("cart-owner")
        store = Store.objects.create(owner=owner, name="Cart Shop", slug="cart-shop")
        product = Product.objects.create(store=store, name="Kopi", slug="kopi", sku="K-1", price=1000)
        warehouse = Warehouse.objects.create(store=store, name="Jakarta", code="JKT")
        InventoryLevel.objects.create(warehouse=warehouse, product=product, on_hand=1)
        client = Client()
        client.post(f"/cart/add/{product.id}/")
        response = client.get("/cart/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="recovery_email" value=""')
