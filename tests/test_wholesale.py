"""Harga grosir: quantity tiers per product, counted over all its variants, in the cart, at
checkout, on manual orders, and on the product page; never combined with a flash sale."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import (
    FlashSaleProduct,
    InventoryLevel,
    Product,
    ProductVariant,
    Warehouse,
    WholesalePrice,
)
from apps.orders.models import Order
from apps.shops.models import MarketingCampaign, Store

FORM = {"customer_name": "Bu Sri", "phone": "0812-3456-7890", "fulfilment": "ship",
    "shipping_address": "Apartemen Metro Park Residence Tower B Unit 05", "city": "Jakarta Barat",
    "shipping_name": "JNE REG", "shipping_cost": "15000", "discount": "0",
    "payment_method": "gateway", "valid_days": "2", "idempotency_key": "grosir-1"}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class WholesaleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("grosir-owner")
        self.store = Store.objects.create(owner=self.owner, name="Grosir", slug="grosir")
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000)
        InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=100)
        WholesalePrice.objects.create(product=self.kopi, min_quantity=10, price=45000)
        WholesalePrice.objects.create(product=self.kopi, min_quantity=50, price=40000)
        self.kaos = Product.objects.create(store=self.store, name="Kaos", slug="kaos", sku="KAOS",
            price=100000)
        self.kaos_s = ProductVariant.objects.create(product=self.kaos, name="S", sku="KAOS-S",
            stock=50)
        self.kaos_xl = ProductVariant.objects.create(product=self.kaos, name="XL", sku="KAOS-XL",
            price=120000, stock=50)
        WholesalePrice.objects.create(product=self.kaos, min_quantity=6, price=80000)
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _cart(self, items):
        session = self.client.session
        session["cart"] = items
        session.save()
        return self.client.get("/cart/")

    def test_tier_follows_quantity(self):
        for quantity, price in ((9, 50000), (10, 45000), (49, 45000), (50, 40000)):
            page = self._cart({f"p:{self.kopi.id}": quantity})
            line = page.context["lines"][0]
            self.assertEqual(line["unit_price"], price, quantity)
            self.assertEqual(line["total"], price * quantity)
        self.assertContains(page, "Harga grosir (≥ 50 pcs)")

    def test_variants_count_together_and_scale_by_percentage(self):
        page = self._cart({f"v:{self.kaos_s.id}": 3, f"v:{self.kaos_xl.id}": 3})
        prices = {line["variant"].sku: line["unit_price"] for line in page.context["lines"]}
        # 6 shirts in total: S drops to the tier price, XL by the same 20%.
        self.assertEqual(prices, {"KAOS-S": 80000, "KAOS-XL": 96000})
        self.assertEqual(page.context["subtotal"], 3 * 80000 + 3 * 96000)

    def test_checkout_snapshots_the_wholesale_price(self):
        self._cart({f"p:{self.kopi.id}": 12})
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        order = Order.objects.get()
        item = order.items.get()
        self.assertEqual((item.unit_price, item.line_total), (45000, 540000))
        self.assertEqual(order.subtotal, 540000)

    def test_flash_sale_is_not_combined(self):
        campaign = MarketingCampaign.objects.create(store=self.store, name="Flash",
            headline="Flash", starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1))
        FlashSaleProduct.objects.create(campaign=campaign, product=self.kopi, sale_price=48000)
        line = self._cart({f"p:{self.kopi.id}": 50}).context["lines"][0]
        self.assertEqual((line["unit_price"], line["wholesale"]), (48000, None))
        self.assertContains(self.client.get("/products/kopi/"), "Tidak berlaku selama flash sale")

    def test_product_page_and_api_show_tiers(self):
        page = self.client.get("/products/kopi/")
        self.assertContains(page, "Harga grosir")
        self.assertContains(page, "Beli ≥ 10")
        self.assertContains(page, "Rp40.000")
        api = self.client.get("/api/v1/products/kopi/", HTTP_X_STORE_SLUG="grosir")
        self.assertEqual(api.json()["data"]["wholesale_prices"][0],
            {"min_quantity": 10, "price": "45000.00"})

    def test_manual_order_uses_tiers_unless_a_price_is_typed(self):
        data = {**FORM, "sku": ["KOPI", "KAOS-S", "KAOS-XL"], "quantity": ["10", "4", "2"],
            "price": ["", "", "110000"]}
        self.seller.post("/merchant/orders/new/", data)
        items = {item.sku: item.unit_price for item in Order.objects.get().items.all()}
        self.assertEqual(items, {"KOPI": 45000, "KAOS-S": 80000, "KAOS-XL": 110000})


class WholesaleFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("grosir-seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        Warehouse.objects.create(store=self.store, name="Gudang", code="MAIN")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=50000)
        self.seller = Client()
        self.seller.force_login(self.owner)
        self.url = f"/merchant/setup/products/{self.product.id}/"

    def _save(self, **extra):
        data = {"name": "Kopi", "slug": "kopi", "sku": "KOPI", "price": "50000",
            "is_active": "on", "weight_grams": 500, "length_cm": 10, "width_cm": 10,
            "height_cm": 10, "preorder_days": 1, **extra}
        return self.seller.post(self.url, data)

    def _tiers(self):
        return list(self.product.wholesale_prices.values_list("min_quantity", "price"))

    def test_seller_sets_edits_and_clears_tiers(self):
        self.assertContains(self.seller.get(self.url), "Harga grosir")
        reply = self._save(wholesale_min_1="50", wholesale_price_1="40000",
            wholesale_min_2="10", wholesale_price_2="45000")
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(self._tiers(), [(10, Decimal(45000)), (50, Decimal(40000))])
        page = self.seller.get(self.url)
        self.assertEqual(page.context["form"]["wholesale_min_1"].value(), 10)
        self._save(wholesale_min_1="10", wholesale_price_1="44000")
        self.assertEqual(self._tiers(), [(10, Decimal(44000))])
        self._save()
        self.assertEqual(self._tiers(), [])

    def test_invalid_tiers_are_refused(self):
        cases = [
            ({"wholesale_min_1": "10", "wholesale_price_1": "50000"}, "lebih murah"),
            ({"wholesale_min_1": "10", "wholesale_price_1": "40000",
              "wholesale_min_2": "20", "wholesale_price_2": "42000"}, "lebih murah"),
            ({"wholesale_min_1": "10", "wholesale_price_1": "40000",
              "wholesale_min_2": "10", "wholesale_price_2": "39000"}, "dipakai dua kali"),
            ({"wholesale_min_1": "10"}, "Isi jumlah minimal dan harganya"),
            ({"wholesale_min_1": "1", "wholesale_price_1": "40000"}, "2"),
        ]
        for data, message in cases:
            page = self._save(**data)
            self.assertEqual(page.status_code, 200, data)
            self.assertContains(page, message)
        self.assertEqual(self._tiers(), [])
