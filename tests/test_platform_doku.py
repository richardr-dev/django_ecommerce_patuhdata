"""DOKU Lakuin: a store without its own DOKU account takes QRIS / VA / e-wallet through the
platform's account; Lakuin tracks the store's balance and pays it out to the store's bank."""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.models import PlatformPayout, StorePaymentSettings
from apps.integrations.payments import payment_options
from apps.integrations.platform_balance import balance
from apps.orders.models import Order, ShippingMethod
from apps.shops.models import Store

PLATFORM = {"PAYMENT_PROVIDER": "doku", "DOKU_CLIENT_ID": "MCH-PLATFORM",
    "DOKU_SECRET_KEY": "platform-secret", "PLATFORM_COLLECTS_PAYMENTS": False,
    "PLATFORM_DOKU_FOR_STORES": True, "PLATFORM_DOKU_FEE_PERCENT": Decimal(0),
    "SHIPPING_RATE_PROVIDER": "biteship", "BITESHIP_API_KEY": ""}


@override_settings(**PLATFORM)
class PlatformDokuTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.account = StorePaymentSettings.objects.create(store=self.store, bank_name="BCA",
            bank_account_number="1234567890", bank_account_holder="Ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _order(self, status, total=100000):
        return Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=total, total=total, status=status,
            payment_provider="doku", paid_to_store_account=False,
            idempotency_key=uuid.uuid4().hex)

    def test_the_seller_opts_in_and_checkout_offers_doku_through_lakuin(self):
        self.assertEqual(payment_options(self.store), [])
        page = self.seller.get("/merchant/payments/")
        self.assertContains(page, "DOKU Lakuin")
        self.seller.post("/merchant/payments/", {"bank_name": "BCA",
            "bank_account_number": "1234567890", "bank_account_holder": "Ruuma",
            "use_platform_gateway": "on"})
        self.account.refresh_from_db()
        self.assertTrue(self.account.use_platform_gateway)
        option = payment_options(self.store)[0]
        self.assertEqual((option.provider, option.store_account), ("doku", False))

    def test_opting_in_needs_a_bank_account_for_payouts(self):
        page = self.seller.post("/merchant/payments/", {"use_platform_gateway": "on"})
        self.assertContains(page, "rekening bank tujuan pencairan")
        self.account.refresh_from_db()
        self.assertFalse(self.account.use_platform_gateway)

    @override_settings(PLATFORM_DOKU_FOR_STORES=False)
    def test_the_platform_can_switch_it_off(self):
        StorePaymentSettings.objects.filter(id=self.account.id).update(use_platform_gateway=True)
        self.assertEqual(payment_options(self.store), [])
        self.assertNotContains(self.seller.get("/merchant/payments/"), "3. DOKU Lakuin")

    @patch("apps.integrations.payments.request_json",
        return_value={"response": {"payment": {"url": "https://doku.test/pay"}}})
    def test_a_checkout_is_charged_to_the_platform_account(self, request_json):
        StorePaymentSettings.objects.filter(id=self.account.id).update(use_platform_gateway=True)
        product = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="K",
            price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="G", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=product, on_hand=3)
        ShippingMethod.objects.create(store=self.store, code="reg", name="Reguler", flat_rate=0)
        buyer = Client()
        buyer.post(f"/cart/add/{product.id}/")
        reply = buyer.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "shipping_method": "reg", "payment_method": "gateway"})
        self.assertEqual(reply.url, "https://doku.test/pay")
        order = Order.objects.get()
        self.assertFalse(order.paid_to_store_account)
        self.assertEqual(request_json.call_args.kwargs["headers"]["Client-Id"], "MCH-PLATFORM")

    @override_settings(PLATFORM_DOKU_FEE_PERCENT=Decimal("2.5"))
    def test_balance_releases_after_delivery_less_fee_and_payouts(self):
        self._order("shipped", 40000)
        self._order("completed", 100000)
        self._order("cancelled", 70000)
        Order.objects.create(store=self.store, customer_name="B", phone="1", shipping_address="x",
            subtotal=1, total=900000, status="completed", payment_provider="doku",
            paid_to_store_account=True, idempotency_key="own")  # paid to the seller's own DOKU
        summary = balance(self.store)
        self.assertEqual((summary["pending"], summary["earned"], summary["fees"]),
            (Decimal(39000), Decimal(97500), Decimal(2500)))
        self.assertEqual(summary["available"], Decimal(97500))

    def test_the_seller_requests_a_payout_once(self):
        self._order("delivered", 60000)
        page = self.seller.get("/merchant/payments/balance/")
        self.assertContains(page, "Rp60.000")
        self.seller.post("/merchant/payments/balance/")
        self.seller.post("/merchant/payments/balance/")  # nothing left to request
        payout = PlatformPayout.objects.get()
        self.assertEqual((payout.amount, payout.bank_account_number, payout.status),
            (Decimal(60000), "1234567890", "requested"))
        self.assertEqual(balance(self.store)["available"], 0)

        PlatformPayout.objects.filter(id=payout.id).update(status="rejected")
        self.assertEqual(balance(self.store)["available"], Decimal(60000))

    def test_small_balances_wait_for_the_minimum(self):
        self._order("delivered", 5000)
        self.seller.post("/merchant/payments/balance/")
        self.assertFalse(PlatformPayout.objects.exists())
