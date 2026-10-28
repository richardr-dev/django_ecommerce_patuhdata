"""Race-condition tests that need real row locks.

SQLite ignores SELECT ... FOR UPDATE, so these run only against PostgreSQL, e.g.:

    DATABASE_URL=postgresql://commerce:commerce@localhost:5432/commerce pytest tests/test_postgres_concurrency.py

Each test pauses inside the locked section so that, without working locks, both requests
would read the same stock/coupon/refund state before either commits.
"""
import threading
import time
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import Client, TransactionTestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders import loyalty
from apps.orders.models import Coupon, Order, PaymentTransaction, Refund
from apps.orders.refunds import RefundError, create_refund_request
from apps.orders.services import transition_order
from apps.shops.models import Store

PAUSE_SECONDS = 0.5
FORM = {"customer_name": "Ayu", "phone": "081234567890", "shipping_address": "Jakarta"}


def run_concurrently(*callables):
    barrier = threading.Barrier(len(callables))
    results = [None] * len(callables)

    def worker(index, function):
        try:
            barrier.wait()
            results[index] = function()
        except Exception as error:  # noqa: BLE001 - surfaced to the asserting test
            results[index] = error
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(index, function))
        for index, function in enumerate(callables)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def slow_redeem(order, points):
    """Runs inside the checkout transaction after stock and coupon rows are locked."""
    time.sleep(PAUSE_SECONDS)
    return loyalty.redeem_for_order(order, points)


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class PostgresCheckoutRaceTests(TransactionTestCase):
    def setUp(self):
        owner = User.objects.create_user("race-owner")
        self.owner = owner
        self.store = Store.objects.create(owner=owner, name="Race Shop", slug="race-shop")
        self.product = Product.objects.create(store=self.store, name="Last", slug="last",
            sku="LAST-1", price=10000)
        warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.product,
            on_hand=1)

    def _buyer(self, name, product=None, data=None):
        client = Client()
        client.force_login(User.objects.create_user(name))
        client.post(f"/cart/add/{(product or self.product).id}/", data or {})
        return lambda: client.post("/checkout/", {**FORM, **(self.extra_form or {})})

    extra_form = None

    @patch("apps.orders.views.redeem_for_order", slow_redeem)
    def test_last_warehouse_unit_is_sold_once(self):
        results = run_concurrently(self._buyer("buyer-a"), self._buyer("buyer-b"))
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.level.refresh_from_db()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.level.reserved, 1)

    @patch("apps.orders.views.redeem_for_order", slow_redeem)
    def test_last_variant_unit_is_sold_once(self):
        variant = ProductVariant.objects.create(product=self.product, name="Merah", sku="LAST-RED",
            stock=1)
        results = run_concurrently(
            self._buyer("buyer-c", data={"variant_id": variant.id}),
            self._buyer("buyer-d", data={"variant_id": variant.id}))
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        variant.refresh_from_db()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(variant.reserved, 1)

    @patch("apps.orders.views.redeem_for_order", slow_redeem)
    def test_single_use_coupon_is_redeemed_once(self):
        self.level.on_hand = 10
        self.level.save()
        coupon = Coupon.objects.create(store=self.store, code="SEKALI", kind="fixed", value=1000,
            usage_limit=1)
        self.extra_form = {"coupon_code": "SEKALI"}
        results = run_concurrently(self._buyer("buyer-e"), self._buyer("buyer-f"))
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(Order.objects.filter(coupon_code="SEKALI").count(), 1)

    def test_concurrent_refunds_cannot_exceed_payment(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(100000), total=Decimal(100000),
            idempotency_key="race-refund")
        PaymentTransaction.objects.create(order=order, provider="demo", external_id="race-pay",
            status="succeeded", reconciled=True, amount=Decimal(100000))
        transition_order(order, "paid")
        original_create = Refund.objects.create

        def slow_create(**kwargs):
            time.sleep(PAUSE_SECONDS)
            return original_create(**kwargs)

        def refund():
            return create_refund_request(order=order, amount="80000", reason="OTHERS",
                actor=self.owner)

        with patch.object(Refund.objects, "create", side_effect=slow_create):
            results = run_concurrently(refund, refund)
        self.assertEqual(sum(isinstance(r, Refund) for r in results), 1, results)
        self.assertEqual(sum(isinstance(r, RefundError) for r in results), 1, results)
        self.assertEqual(Refund.objects.filter(order=order).count(), 1)
