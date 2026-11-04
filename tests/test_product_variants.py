"""Variants are edited on their product's page, each with its own SKU, price and stock, instead
of on a separate list where the seller types the parent product's SKU."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import InventoryMovement
from apps.shops.models import Store

PRODUCT = {"name": "Kaos", "slug": "kaos", "sku": "KAOS", "price": "100000", "is_active": "on",
    "weight_grams": 200, "length_cm": 10, "width_cm": 10, "height_cm": 2, "preorder_days": 1}


def variant_rows(rows, initial=0):
    data = {"variants-TOTAL_FORMS": str(len(rows)), "variants-INITIAL_FORMS": str(initial),
        "variants-MIN_NUM_FORMS": "0", "variants-MAX_NUM_FORMS": "1000"}
    for index, row in enumerate(rows):
        data.update({f"variants-{index}-{key}": value for key, value in row.items()})
    return data


class ProductVariantTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.warehouse = Warehouse.objects.create(store=self.store, name="Gudang Utama", code="MAIN")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def test_a_new_product_is_created_with_its_variants(self):
        reply = self.seller.post("/merchant/setup/products/new/", {**PRODUCT, "initial_stock": 5,
            **variant_rows([
                {"name": "S", "sku": "KAOS-S", "stock": 4, "is_active": "on", "attributes": '{"Ukuran": "S"}'},
                {"name": "M", "sku": "KAOS-M", "price": "110000", "stock": 6, "is_active": "on"},
            ])})
        self.assertEqual(reply.status_code, 302)
        product = Product.objects.get(sku="KAOS")
        small, medium = product.variants.order_by("id")
        self.assertEqual((small.sku, small.stock, small.attributes), ("KAOS-S", 4, {"Ukuran": "S"}))
        self.assertEqual((medium.effective_price, medium.stock), (110000, 6))
        # Stock lives on the variants, so no warehouse stock is created for the product itself.
        self.assertFalse(InventoryLevel.objects.filter(product=product).exists())
        self.assertEqual(InventoryMovement.objects.filter(variant__product=product).count(), 2)

    def test_editing_variant_stock_on_the_product_page_is_logged(self):
        product = Product.objects.create(store=self.store, sku="KAOS", slug="kaos", name="Kaos", price=100000)
        variant = ProductVariant.objects.create(product=product, name="S", sku="KAOS-S", stock=3, reserved=1)
        page = self.seller.get(f"/merchant/setup/products/{product.id}/")
        self.assertContains(page, 'id="variants"')
        self.assertContains(page, "KAOS-S")
        reply = self.seller.post(f"/merchant/setup/products/{product.id}/", {**PRODUCT, **variant_rows(
            [{"id": variant.id, "name": "S", "sku": "KAOS-S", "stock": 9, "is_active": "on"}], initial=1)})
        self.assertEqual(reply.status_code, 302)
        variant.refresh_from_db()
        self.assertEqual((variant.stock, variant.reserved), (9, 1))
        movement = InventoryMovement.objects.get(variant=variant)
        self.assertEqual((movement.previous_on_hand, movement.new_on_hand, movement.actor), (3, 9, self.owner))

    def test_variant_stock_cannot_drop_below_reserved_units(self):
        product = Product.objects.create(store=self.store, sku="KAOS", slug="kaos", name="Kaos", price=100000)
        variant = ProductVariant.objects.create(product=product, name="S", sku="KAOS-S", stock=5, reserved=4)
        reply = self.seller.post(f"/merchant/setup/products/{product.id}/", {**PRODUCT, **variant_rows(
            [{"id": variant.id, "name": "S", "sku": "KAOS-S", "stock": 2, "is_active": "on"}], initial=1)})
        self.assertEqual(reply.status_code, 200)
        variant.refresh_from_db()
        self.assertEqual(variant.stock, 5)

    def test_variant_skus_must_be_unique_in_the_store(self):
        Product.objects.create(store=self.store, sku="TOPI", slug="topi", name="Topi", price=50000)
        reply = self.seller.post("/merchant/setup/products/new/", {**PRODUCT, **variant_rows([
            {"name": "S", "sku": "TOPI", "stock": 1, "is_active": "on"},
            {"name": "M", "sku": "KAOS-M", "stock": 1, "is_active": "on"},
            {"name": "L", "sku": "KAOS-M", "stock": 1, "is_active": "on"},
            {"name": "XL", "sku": "KAOS", "stock": 1, "is_active": "on"},
        ])})
        self.assertEqual(reply.status_code, 200)
        self.assertContains(reply, "sudah dipakai produk atau varian lain")
        self.assertContains(reply, "dipakai dua kali")
        self.assertContains(reply, "tidak boleh sama dengan SKU produk")
        self.assertFalse(Product.objects.filter(sku="KAOS").exists())

    def test_deleting_a_variant(self):
        product = Product.objects.create(store=self.store, sku="KAOS", slug="kaos", name="Kaos", price=100000)
        variant = ProductVariant.objects.create(product=product, name="S", sku="KAOS-S", stock=3)
        self.seller.post(f"/merchant/setup/products/{product.id}/", {**PRODUCT, **variant_rows(
            [{"id": variant.id, "name": "S", "sku": "KAOS-S", "stock": 3, "is_active": "on",
              "DELETE": "on"}], initial=1)})
        self.assertFalse(ProductVariant.objects.filter(id=variant.id).exists())

    def test_the_standalone_variant_pages_lead_to_the_product(self):
        product = Product.objects.create(store=self.store, sku="KAOS", slug="kaos", name="Kaos", price=100000)
        variant = ProductVariant.objects.create(product=product, name="S", sku="KAOS-S")
        self.assertRedirects(self.seller.get("/merchant/setup/variants/"), "/merchant/setup/products/")
        reply = self.seller.get(f"/merchant/setup/variants/{variant.id}/")
        self.assertEqual(reply["Location"], f"/merchant/setup/products/{product.id}/#variants")
        self.assertNotContains(self.seller.get("/merchant/"), "/merchant/setup/variants/")

    def test_the_product_list_counts_variants_and_finds_them_by_sku(self):
        product = Product.objects.create(store=self.store, sku="KAOS", slug="kaos", name="Kaos", price=100000)
        ProductVariant.objects.create(product=product, name="S", sku="KAOS-S")
        ProductVariant.objects.create(product=product, name="M", sku="KAOS-M")
        page = self.seller.get("/merchant/setup/products/?q=KAOS-M")
        self.assertContains(page, "Varian")
        self.assertContains(page, "Kaos")
