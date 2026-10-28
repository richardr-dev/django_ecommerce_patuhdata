from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import FlashSaleProduct, InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.forms import FlashSaleProductForm
from apps.orders.models import Order
from apps.orders.services import transition_order
from apps.shops.models import MarketingCampaign, Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class FlashSaleEdgeCaseTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("edge-owner")
        self.buyer = User.objects.create_user("edge-buyer", password="test")
        self.store = Store.objects.create(owner=owner, name="Edge Shop", slug="edge-shop")
        self.product = Product.objects.create(store=self.store, name="Edge Product",
            slug="edge-product", sku="EDGE-1", price=100000)
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)
        self.campaign = MarketingCampaign.objects.create(store=self.store, name="Edge Sale",
            headline="Edge sale", starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=2))
        self.offer = FlashSaleProduct.objects.create(campaign=self.campaign, product=self.product,
            sale_price=70000, stock_limit=1)
        self.client.force_login(self.buyer)

    def test_cancelling_order_reopens_campaign_allocation(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        order = Order.objects.get()
        self.client.post(f"/cart/add/{self.product.id}/")
        self.assertNotContains(self.client.get("/cart/"), "Rp70.000 per item")
        transition_order(order, "cancelled", note="Customer cancelled")
        self.client.post(f"/cart/add/{self.product.id}/")
        self.assertContains(self.client.get("/cart/"), "Rp70.000 per item")

    def test_price_change_cannot_turn_sale_into_price_increase(self):
        self.product.price = 60000
        self.product.save(update_fields=["price"])
        home = self.client.get("/")
        self.assertNotContains(home, "Harga turun, waktu terbatas")
        self.client.post(f"/cart/add/{self.product.id}/")
        self.assertContains(self.client.get("/cart/"), "Rp60.000 per item")

    def test_sale_price_must_be_below_every_active_variant(self):
        ProductVariant.objects.create(product=self.product, name="Small", sku="EDGE-S",
            price=65000, stock=2)
        form = FlashSaleProductForm(data={"campaign": self.campaign.id,
            "product": self.product.sku, "sale_price": "70000", "stock_limit": "",
            "per_customer_limit": ""}, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("sale_price", form.errors)
