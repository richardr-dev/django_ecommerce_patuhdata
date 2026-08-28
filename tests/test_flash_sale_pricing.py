from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import FlashSaleProduct, InventoryLevel, Product, Warehouse
from apps.orders.models import Order
from apps.shops.models import MarketingCampaign, Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class FlashSalePricingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("flash-owner")
        self.buyer = User.objects.create_user("flash-buyer", password="test")
        self.store = Store.objects.create(owner=self.owner, name="Flash Shop", slug="flash-shop")
        self.product = Product.objects.create(store=self.store, name="Flash Product",
            slug="flash-product", sku="FLASH-PRODUCT", price=100000)
        warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=10)
        self.campaign = MarketingCampaign.objects.create(store=self.store, name="Flash",
            headline="Flash sale", starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=3))
        self.offer = FlashSaleProduct.objects.create(campaign=self.campaign, product=self.product,
            sale_price=70000, stock_limit=2, per_customer_limit=1)
        self.client.force_login(self.buyer)

    def test_active_sale_price_is_used_everywhere_and_snapshotted(self):
        home = self.client.get("/")
        self.assertContains(home, "Harga turun, waktu terbatas")
        self.assertContains(home, "Rp100.000")
        self.assertContains(home, "Rp70.000")
        self.client.post(f"/cart/add/{self.product.id}/")
        cart = self.client.get("/cart/")
        self.assertContains(cart, "Rp70.000 per item")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        item = Order.objects.get().items.get()
        self.assertEqual(item.unit_price, 70000)
        self.assertEqual(item.flash_sale_offer, self.offer)

    def test_per_customer_limit_is_enforced_by_checkout(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.post("/checkout/", {"customer_name": "Ayu",
            "phone": "081234567890", "shipping_address": "Jakarta"}, follow=True)
        self.assertContains(response, "Batas pembelian flash sale")
        self.assertEqual(Order.objects.count(), 1)

    def test_expired_campaign_restores_normal_price(self):
        self.campaign.ends_at = timezone.now() - timedelta(seconds=1)
        self.campaign.save(update_fields=["ends_at"])
        self.client.post(f"/cart/add/{self.product.id}/")
        cart = self.client.get("/cart/")
        self.assertContains(cart, "Rp100.000 per item")
        self.assertNotContains(self.client.get("/"), "Harga turun, waktu terbatas")
