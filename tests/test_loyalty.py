from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.loyalty import redeem_for_order, redemption_quote
from apps.orders.models import LoyaltyAccount, LoyaltyTransaction, Order, ShippingMethod
from apps.orders.services import transition_order
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class LoyaltyProgramTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("rewards-owner")
        self.buyer = User.objects.create_user("rewards-buyer")
        self.store = Store.objects.create(
            owner=owner,
            name="Rewards Shop",
            slug="rewards-shop",
            loyalty_enabled=True,
            loyalty_points_per_step=2,
            loyalty_spend_step=10000,
            loyalty_point_value=100,
            loyalty_min_redeem=10,
        )
        self.product = Product.objects.create(
            store=self.store,
            name="Reward Product",
            slug="reward-product",
            sku="REWARD-1",
            price=100000,
        )
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=10)
        ShippingMethod.objects.create(store=self.store, code="pickup", name="Pickup", flat_rate=0)

    def order(self, status="pending", **kwargs):
        values = {
            "store": self.store,
            "customer_user": self.buyer,
            "status": status,
            "customer_name": "Ayu",
            "email": "ayu@example.com",
            "phone": "081234567890",
            "shipping_address": "Jakarta",
            "subtotal": Decimal(100000),
            "total": Decimal(100000),
            "idempotency_key": kwargs.pop("idempotency_key", f"loyalty-{Order.objects.count()}"),
        }
        values.update(kwargs)
        return Order.objects.create(**values)

    def test_completed_order_earns_once(self):
        order = self.order(status="delivered")
        self.assertTrue(transition_order(order, "completed"))
        account = LoyaltyAccount.objects.get(store=self.store, user=self.buyer)
        self.assertEqual(account.balance, 20)
        self.assertEqual(account.lifetime_earned, 20)
        self.assertEqual(LoyaltyTransaction.objects.filter(order=order, kind="earn").count(), 1)
        self.assertFalse(transition_order(order, "completed"))
        account.refresh_from_db()
        self.assertEqual(account.balance, 20)

    def test_redemption_is_capped_and_cancel_restores_points(self):
        account = LoyaltyAccount.objects.create(
            store=self.store, user=self.buyer, balance=50, lifetime_earned=50
        )
        points, discount = redemption_quote(
            self.store, self.buyer, 500, Decimal(3000), account=account
        )
        self.assertEqual((points, discount), (30, Decimal(3000)))
        order = self.order(
            loyalty_points_redeemed=points,
            loyalty_discount=discount,
            discount=discount,
            total=Decimal(97000),
        )
        redeem_for_order(order, points)
        account.refresh_from_db()
        self.assertEqual(account.balance, 20)
        transition_order(order, "cancelled")
        account.refresh_from_db()
        self.assertEqual(account.balance, 50)

    def test_refund_reverses_reward_even_after_points_were_spent(self):
        order = self.order(status="delivered")
        transition_order(order, "completed")
        account = LoyaltyAccount.objects.get(store=self.store, user=self.buyer)
        account.balance = 0
        account.save(update_fields=["balance"])
        transition_order(order, "refund_requested")
        transition_order(order, "refunded")
        account.refresh_from_db()
        self.assertEqual(account.balance, -20)
        self.assertEqual(LoyaltyTransaction.objects.get(order=order, kind="reversal").points, -20)

    def test_below_minimum_redemption_is_ignored(self):
        account = LoyaltyAccount.objects.create(store=self.store, user=self.buyer, balance=50)
        self.assertEqual(
            redemption_quote(self.store, self.buyer, 9, Decimal(100000), account=account),
            (0, Decimal(0)),
        )

    def test_checkout_redeems_points_and_uses_discount(self):
        LoyaltyAccount.objects.create(store=self.store, user=self.buyer, balance=50)
        self.client.force_login(self.buyer)
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.post(
            "/checkout/",
            {
                "customer_name": "Ayu",
                "phone": "081234567890",
                "shipping_address": "Jakarta",
                "shipping_method": "pickup",
                "loyalty_points": "25",
            },
        )
        order = Order.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.loyalty_points_redeemed, 25)
        self.assertEqual(order.loyalty_discount, Decimal(2500))
        self.assertEqual(order.total, Decimal(97500))
        self.assertEqual(LoyaltyAccount.objects.get(store=self.store, user=self.buyer).balance, 25)
