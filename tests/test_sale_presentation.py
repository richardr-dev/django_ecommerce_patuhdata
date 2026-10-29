from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.catalog.models import Product
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class SalePresentationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("sale-owner")
        self.store = Store.objects.create(owner=owner, name="Orange Shop", slug="orange-shop")
        self.sale = Product.objects.create(store=self.store, name="Sale Product", slug="sale-product",
            sku="SALE-1", price=75000, compare_at_price=100000)

    def test_new_store_uses_orange_default(self):
        self.assertEqual(self.store.primary_color, "#f97316")

    def test_sale_price_has_red_strike_and_percentage_badge(self):
        self.assertTrue(self.sale.has_discount)
        self.assertEqual(self.sale.discount_percent, 25)
        response = self.client.get("/")
        self.assertContains(response, "card-sale-badge")
        self.assertContains(response, "-25%")
        self.assertContains(response, "<del>Rp100.000</del>", html=True)
        detail = self.client.get("/products/sale-product/")
        self.assertContains(detail, "Hemat 25%")
        self.assertContains(detail, "<del>Rp100.000</del>", html=True)

    def test_invalid_compare_price_is_not_presented_as_a_sale(self):
        product = Product.objects.create(store=self.store, name="Not Sale", slug="not-sale",
            sku="NOT-SALE", price=100000, compare_at_price=90000)
        self.assertFalse(product.has_discount)
        response = self.client.get("/products/not-sale/")
        self.assertNotContains(response, "Harga spesial")
        # Only the product's own price panel; related-product cards may show other sales.
        price_panel = response.content.decode().split('class="product-price-panel"')[1].split("</div>")[0]
        self.assertNotIn("<del>", price_panel)
