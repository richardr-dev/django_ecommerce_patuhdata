"""Buyers pay each seller's own accounts: bank transfer from day one, or the seller's own
DOKU/Xendit account. The platform account takes store payments only when explicitly allowed."""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.accounting.ledger import CASH, post_pending_entries
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.forms import PaymentSettingsForm
from apps.integrations.models import StorePaymentSettings
from apps.integrations.payments import doku_signature, payment_options
from apps.orders.models import Order, PaymentTransaction
from apps.shops.models import Store

BUYER = {"customer_name": "Ayu", "email": "ayu@example.com", "phone": "081234567890",
    "shipping_address": "Jakarta"}


def _bank(store):
    return StorePaymentSettings.objects.create(store=store, bank_transfer_enabled=True,
        bank_name="BCA", bank_account_number="1234567890", bank_account_holder="Toko Ayu")


def _doku(store, client_id="MCH-SELLER", secret="seller-secret"):
    account = StorePaymentSettings(store=store, gateway="doku", doku_client_id=client_id)
    account.secret_key = secret
    account.save()
    return account


@override_settings(PAYMENT_PROVIDER="doku", DOKU_CLIENT_ID="MCH-PLATFORM",
    DOKU_SECRET_KEY="platform-secret", PLATFORM_COLLECTS_PAYMENTS=False,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="", PUBLIC_BASE_URL="https://lakuin.test")
class StorePaymentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Toko Ayu", slug="toko-ayu")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)
        self.buyer = Client()
        self.buyer.force_login(User.objects.create_user("buyer"))

    def _checkout(self, **extra):
        self.buyer.post(f"/cart/add/{self.product.id}/")
        return self.buyer.post("/checkout/", {**BUYER, **extra})

    def test_platform_account_is_not_offered_to_stores(self):
        self.assertEqual(payment_options(self.store), [])

        response = self._checkout()
        self.assertContains(response, "belum mengaktifkan metode pembayaran")
        self.assertFalse(Order.objects.exists())

    @override_settings(PLATFORM_COLLECTS_PAYMENTS=True)
    def test_platform_account_only_when_explicitly_allowed(self):
        [option] = payment_options(self.store)
        self.assertEqual((option.provider, option.store_account), ("doku", False))

    @override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=False)
    def test_disabled_demo_is_not_offered_in_production(self):
        self.assertEqual(payment_options(self.store), [])

    def test_bank_transfer_end_to_end(self):
        _bank(self.store)

        response = self._checkout(payment_method="bank_transfer")
        order = Order.objects.get()
        self.assertEqual((order.payment_provider, order.paid_to_store_account),
            ("bank_transfer", True))
        self.assertTrue(response.url.endswith(f"/orders/{order.id}/?token={order.access_token}"))
        page = self.buyer.get(f"/orders/{order.id}/")
        self.assertContains(page, "1234567890")
        self.assertContains(page, order.payment_reference)

        seller = Client()
        seller.force_login(self.owner)
        seller.post(f"/merchant/orders/{order.id}/confirm-transfer/")
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        post_pending_entries()
        payment = PaymentTransaction.objects.get(kind="payment")
        self.assertTrue(payment.reconciled)
        self.assertEqual(payment.journal_entry.lines.get(debit__gt=0).account.code, CASH)

    def test_other_staff_cannot_confirm_someone_elses_transfer(self):
        _bank(self.store)
        self._checkout(payment_method="bank_transfer")
        order = Order.objects.get()
        stranger = User.objects.create_user("stranger")
        Store.objects.create(owner=stranger, name="Lain", slug="lain")
        client = Client()
        client.force_login(stranger)

        self.assertEqual(client.post(f"/merchant/orders/{order.id}/confirm-transfer/").status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    @patch("apps.integrations.payments.request_json")
    def test_checkout_charges_the_sellers_own_doku_account(self, request_json):
        request_json.return_value = {"response": {"payment": {"url": "https://doku.test/pay"}}}
        _doku(self.store)

        response = self._checkout(payment_method="gateway")
        order = Order.objects.get()
        self.assertEqual(response.url, "https://doku.test/pay")
        self.assertTrue(order.paid_to_store_account)
        self.assertEqual(request_json.call_args.kwargs["headers"]["Client-Id"], "MCH-SELLER")

    def _doku_notification(self, order, path, secret):
        body = json.dumps({"order": {"invoice_number": order.payment_reference,
            "amount": int(order.total), "currency": "IDR"}, "transaction": {"status": "SUCCESS",
            "original_request_id": "seller-request"}}, separators=(",", ":")).encode()
        timestamp = "2026-11-02T10:00:00Z"
        return Client().post(path, data=body, content_type="application/json",
            HTTP_CLIENT_ID="MCH-SELLER", HTTP_REQUEST_ID="notif-1", HTTP_REQUEST_TIMESTAMP=timestamp,
            HTTP_SIGNATURE=doku_signature("MCH-SELLER", "notif-1", timestamp, path, body, secret))

    def test_store_webhook_is_verified_with_the_sellers_secret(self):
        account = _doku(self.store)
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(50000), total=Decimal(50000),
            payment_provider="doku", payment_reference="NKSELLER", paid_to_store_account=True,
            idempotency_key="seller-doku")
        path = f"/integrations/webhooks/doku/{account.webhook_key}/"

        forged = self._doku_notification(order, path, "platform-secret")
        accepted = self._doku_notification(order, path, "seller-secret")

        order.refresh_from_db()
        self.assertEqual(forged.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(order.status, "paid")

    def test_store_webhook_cannot_touch_another_stores_orders(self):
        _doku(self.store)
        other = Store.objects.create(owner=User.objects.create_user("other"), name="B", slug="b")
        other_account = _doku(other, secret="other-secret")
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(50000), total=Decimal(50000),
            payment_provider="doku", payment_reference="NKVICTIM", paid_to_store_account=True,
            idempotency_key="victim")

        response = self._doku_notification(order,
            f"/integrations/webhooks/doku/{other_account.webhook_key}/", "other-secret")

        order.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(order.status, "pending")


class PaymentSettingsFormTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("o"), name="S", slug="s")
        self.account = StorePaymentSettings.objects.create(store=self.store)

    def _save(self, **data):
        form = PaymentSettingsForm(data, instance=self.account)
        if form.is_valid():
            form.save()
        return form

    def test_secret_is_encrypted_and_kept_when_left_blank(self):
        self._save(gateway="doku", doku_client_id="MCH-1", secret_key="very-secret")
        self.assertNotIn("very-secret", self.account.encrypted_secret_key)

        self._save(gateway="doku", doku_client_id="MCH-1", bank_name="BCA")
        self.account.refresh_from_db()
        self.assertEqual(self.account.secret_key, "very-secret")

    def test_switching_gateway_requires_new_credentials(self):
        self._save(gateway="doku", doku_client_id="MCH-1", secret_key="doku-secret")

        form = self._save(gateway="xendit", webhook_token="token")
        self.assertIn("secret_key", form.errors)

    def test_bank_transfer_needs_complete_account_details(self):
        form = self._save(bank_transfer_enabled="on", bank_name="BCA")
        self.assertIn("bank_transfer_enabled", form.errors)

        form = self._save(bank_transfer_enabled="on", bank_name="BCA",
            bank_account_number="123-456 7", bank_account_holder="Ayu")
        self.account.refresh_from_db()
        self.assertEqual(self.account.bank_account_number, "1234567")
