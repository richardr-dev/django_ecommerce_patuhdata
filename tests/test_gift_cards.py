"""Gift card: issued by the seller, redeemed at checkout (never below Rp1.000 to pay), and
restored when the order is cancelled."""
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.gift_cards import issue
from apps.orders.models import GiftCard, Order
from apps.orders.services import transition_order
from apps.shops.models import Store

GUEST = {"customer_name": "Ayu", "phone": "0812-3456-7890", "shipping_address": "Jl. Melati 1"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True, PLATFORM_DOMAIN="",
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class GiftCardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="G", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=9)

    def _checkout(self, code, quantity=1):
        shopper = Client()
        for _ in range(quantity):
            shopper.post(f"/cart/add/{self.product.id}/")
        shopper.post("/checkout/", {**GUEST, "gift_card_code": code})
        return Order.objects.order_by("-created_at").first()

    def test_seller_issues_a_card_and_the_recipient_gets_the_code(self):
        seller = Client()
        seller.force_login(self.owner)
        seller.post("/merchant/gift-cards/", {"value": "75000", "recipient_name": "Sri",
            "recipient_email": "sri@example.com"})
        card = GiftCard.objects.get()
        self.assertEqual((card.balance, len(card.code)), (75000, 19))
        self.assertIn(card.code, mail.outbox[0].body)
        self.assertContains(seller.get("/merchant/gift-cards/"), card.code)

    def test_checkout_redeems_part_of_the_card_and_cancel_restores_it(self):
        card = issue(self.store, 30000)
        order = self._checkout(card.code.lower().replace("-", " "))  # typed loosely
        card.refresh_from_db()
        self.assertEqual((order.gift_card, order.gift_card_amount), (card, 30000))
        self.assertEqual(order.total, 20000)
        self.assertEqual(card.balance, 0)
        transition_order(Order.objects.get(pk=order.pk), "cancelled")
        card.refresh_from_db()
        self.assertEqual(card.balance, 30000)
        self.assertEqual(card.entries.filter(kind="restore").count(), 1)

    def test_a_big_card_leaves_the_minimum_to_pay_and_keeps_the_rest(self):
        card = issue(self.store, 500000)
        order = self._checkout(card.code)
        card.refresh_from_db()
        self.assertEqual(order.total, 1000)
        self.assertEqual(card.balance, 500000 - order.gift_card_amount)

    def test_an_unknown_code_does_not_create_an_order(self):
        self.assertIsNone(self._checkout("AAAA-BBBB-CCCC-DDDD"))
