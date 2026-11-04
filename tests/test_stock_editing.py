"""Sellers change stock right on the product page, and products with no stock entry yet show up
on Kelola stok instead of being missing."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import InventoryMovement
from apps.shops.models import Store


class StockEditingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.warehouse = Warehouse.objects.create(store=self.store, name="Gudang Utama", code="MAIN")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000)
        self.seller = Client()
        self.seller.force_login(self.owner)
        self.url = f"/merchant/setup/products/{self.product.id}/"

    def _save(self, **extra):
        data = {"name": "Kopi", "slug": "kopi", "sku": "KOPI", "price": "50000", "is_active": "on",
            "weight_grams": 500, "length_cm": 10, "width_cm": 10, "height_cm": 10,
            "preorder_days": 1, **extra}
        return self.seller.post(self.url, data)

    def test_the_product_page_edits_stock_and_logs_it(self):
        page = self.seller.get(self.url)
        self.assertContains(page, "Stok di Gudang Utama")
        reply = self._save(stock=12)
        self.assertEqual(reply.status_code, 302)
        level = InventoryLevel.objects.get(product=self.product)
        self.assertEqual(level.on_hand, 12)
        movement = InventoryMovement.objects.get()
        self.assertEqual((movement.previous_on_hand, movement.new_on_hand, movement.actor),
            (0, 12, self.owner))

    def test_stock_cannot_drop_below_reserved_units(self):
        InventoryLevel.objects.create(warehouse=self.warehouse, product=self.product, on_hand=10,
            reserved=4)
        page = self._save(stock=2)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(InventoryLevel.objects.get(product=self.product).on_hand, 10)

    def test_products_with_variants_keep_stock_per_variant(self):
        ProductVariant.objects.create(product=self.product, name="250 g", sku="KOPI-250", stock=3)
        self.assertNotContains(self.seller.get(self.url), "Stok di Gudang Utama")

    def test_kelola_stok_lists_products_without_a_stock_entry(self):
        page = self.seller.get("/merchant/stock/")
        self.assertContains(page, "Produk belum punya stok")
        self.assertContains(page, "Kopi")
        self.seller.post("/merchant/stock/", {"target_type": "product",
            "target_id": self.product.id, "stock": 7, "safety_stock": 0, "reason": "Stok awal"})
        self.assertEqual(InventoryLevel.objects.get(product=self.product).on_hand, 7)
        self.assertNotContains(self.seller.get("/merchant/stock/"), "Produk belum punya stok")
