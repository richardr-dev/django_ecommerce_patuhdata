from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.catalog.models import (
    InventoryLevel,
    Product,
    ProductVariant,
    Warehouse,
    annotate_available_stock,
)
from apps.catalog.views import CATALOG_PAGE_SIZE
from apps.orders.forms import VariantForm
from apps.shops.models import Store


def make_products(store, warehouse, start, count):
    products = Product.objects.bulk_create([Product(store=store, name=f"Produk {number:05d}",
        slug=f"produk-{number:05d}", sku=f"SKU-{number:05d}", price=10000 + number)
        for number in range(start, start + count)])
    InventoryLevel.objects.bulk_create([InventoryLevel(warehouse=warehouse, product=product,
        on_hand=10) for product in products])
    return products


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class CatalogScaleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("scale-owner")
        self.store = Store.objects.create(owner=self.owner, name="Scale Shop", slug="scale-shop")
        self.warehouse = Warehouse.objects.create(store=self.store, name="Main", code="MAIN")

    def _home_query_count(self, path="/"):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_homepage_renders_one_page_and_query_count_does_not_grow_with_catalog(self):
        make_products(self.store, self.warehouse, 1, CATALOG_PAGE_SIZE + 6)
        small = self._home_query_count()
        make_products(self.store, self.warehouse, 1000, 200)
        self.assertEqual(self._home_query_count(), small)
        response = self.client.get("/")
        self.assertEqual(len(response.context["products"]), CATALOG_PAGE_SIZE)
        self.assertContains(response, "230 produk")
        self.assertContains(response, 'rel="next"')

    def test_pagination_keeps_filters_and_ordering_is_stable(self):
        make_products(self.store, self.warehouse, 1, CATALOG_PAGE_SIZE + 6)
        first = self.client.get("/?sort=price")
        second = self.client.get("/?sort=price&page=2")
        self.assertContains(first, "sort=price&amp;page=2#catalog")
        names = [p.name for p in first.context["products"]] + [p.name for p in second.context["products"]]
        self.assertEqual(len(names), CATALOG_PAGE_SIZE + 6)
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(names[0], "Produk 00001")
        self.assertNotContains(second, "Produk unggulan")

    def test_out_of_range_page_falls_back_to_last_page(self):
        make_products(self.store, self.warehouse, 1, 3)
        response = self.client.get("/?page=999")
        self.assertEqual(len(response.context["products"]), 3)

    def test_merchant_product_list_paginates_and_annotates_stock(self):
        make_products(self.store, self.warehouse, 1, 120)
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/merchant/setup/products/")
        self.assertEqual(len(response.context["table_rows"]), 50)
        self.assertLess(len(queries), 30)
        self.assertContains(response, "120 data")
        self.assertContains(response, "Stok tersedia")
        response = self.client.get("/merchant/setup/products/?q=SKU-0011")
        self.assertEqual(len(response.context["table_rows"]), 10)

    def test_stock_page_paginates_and_searches(self):
        make_products(self.store, self.warehouse, 1, 70)
        self.client.force_login(self.owner)
        response = self.client.get("/merchant/stock/")
        self.assertEqual(len(response.context["levels"]), 50)
        self.assertContains(response, "<strong>70</strong>")
        response = self.client.get("/merchant/stock/?q=SKU-00007")
        self.assertEqual(len(response.context["levels"]), 1)
        level = response.context["levels"][0]
        response = self.client.post("/merchant/stock/?q=SKU-00007&page=1", {"target_type": "level",
            "target_id": level.id, "stock": "4", "safety_stock": "0", "reason": "Opname"})
        self.assertRedirects(response, "/merchant/stock/?q=SKU-00007&page=1",
            fetch_redirect_response=False)

    def test_sql_stock_annotation_matches_python_property(self):
        plain, with_variants, empty = make_products(self.store, self.warehouse, 1, 3)
        InventoryLevel.objects.filter(product=plain).update(on_hand=10, reserved=3, safety_stock=2)
        second = Warehouse.objects.create(store=self.store, name="Second", code="SEC")
        InventoryLevel.objects.create(warehouse=second, product=plain, on_hand=1, safety_stock=4)
        ProductVariant.objects.create(product=with_variants, name="M", sku="V-M", stock=7, reserved=2)
        ProductVariant.objects.create(product=with_variants, name="L", sku="V-L", stock=1)
        ProductVariant.objects.create(product=with_variants, name="XL", sku="V-XL", stock=50,
            is_active=False)
        InventoryLevel.objects.filter(product=empty).delete()
        annotated = {product.id: product.stock_available
            for product in annotate_available_stock(Product.objects.filter(store=self.store))}
        for product in Product.objects.filter(store=self.store).prefetch_related("inventory_levels", "variants"):
            self.assertEqual(annotated[product.id], product.available_stock, product.name)
        self.assertEqual(annotated, {plain.id: 5, with_variants.id: 6, empty.id: 0})

    def test_dashboard_low_stock_is_counted_in_sql(self):
        products = make_products(self.store, self.warehouse, 1, 12)
        InventoryLevel.objects.filter(product__in=products[:9]).update(on_hand=2)
        self.client.force_login(self.owner)
        response = self.client.get("/merchant/")
        self.assertEqual(response.context["low_stock_count"], 9)
        self.assertEqual(len(response.context["low_stock"]), 8)

    def test_product_picker_uses_store_scoped_sku_not_a_giant_dropdown(self):
        product, = make_products(self.store, self.warehouse, 1, 1)
        other_owner = User.objects.create_user("other-scale-owner")
        other = Store.objects.create(owner=other_owner, name="Other", slug="other-scale")
        Product.objects.create(store=other, name="Foreign", slug="foreign", sku="FOREIGN-1", price=1)
        data = {"name": "Merah", "sku": "VAR-1", "barcode": "", "attributes": "{\"color\": \"merah\"}", "price": "",
            "weight_grams": "", "stock": "1", "is_active": "on"}
        form = VariantForm(data={**data, "product": product.sku}, store=self.store)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["product"], product)
        self.assertNotIn("<option", str(form["product"]))
        form = VariantForm(data={**data, "product": "FOREIGN-1"}, store=self.store)
        self.assertFalse(form.is_valid())
        self.assertIn("product", form.errors)


class BulkImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("import-owner")
        self.store = Store.objects.create(owner=self.owner, name="Import Shop", slug="import-shop")
        self.client.force_login(self.owner)

    def _upload(self, lines):
        content = "name,sku,price,stock,category\n" + "\n".join(lines)
        return self.client.post("/merchant/products/import/", {"file": SimpleUploadedFile(
            "products.csv", content.encode(), content_type="text/csv")})

    def test_imports_more_than_the_old_thousand_row_limit_with_bounded_queries(self):
        lines = [f"Barang {n},B-{n:05d},{1000 + n},{n % 7},Kategori {n % 5}" for n in range(2500)]
        with CaptureQueriesContext(connection) as queries:
            response = self._upload(lines)
        self.assertRedirects(response, "/merchant/setup/products/", fetch_redirect_response=False)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2500)
        self.assertEqual(self.store.categories.count(), 5)
        self.assertEqual(InventoryLevel.objects.filter(product__store=self.store).count(), 2500)
        self.assertLess(len(queries), 150)

    def test_reimport_updates_by_sku_and_preserves_reservations(self):
        self._upload(["Kopi,K-1,10000,5,Kopi", "Teh,T-1,8000,3,"])
        level = InventoryLevel.objects.get(product__sku="K-1")
        level.reserved = 4
        level.save()
        self._upload(["Kopi Baru,K-1,12000,2,Kopi", "Gula,G-1,5000,9,"])
        product = Product.objects.get(store=self.store, sku="K-1")
        self.assertEqual((product.name, product.price), ("Kopi Baru", 12000))
        level.refresh_from_db()
        self.assertEqual(level.on_hand, 4)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 3)

    def test_duplicate_skus_and_variant_collisions_are_rejected_atomically(self):
        response = self._upload(["A,DUP,1000,1,", "B,DUP,2000,1,"])
        self.assertContains(response, "SKU DUP sudah muncul di baris 2")
        product = Product.objects.create(store=self.store, name="Base", slug="base", sku="BASE", price=1)
        ProductVariant.objects.create(product=product, name="M", sku="VAR-M")
        response = self._upload(["Ok,OK-1,1000,1,", "Clash,VAR-M,1000,1,"])
        self.assertContains(response, "SKU VAR-M sudah dipakai varian")
        self.assertFalse(Product.objects.filter(sku="OK-1").exists())

    def test_slugs_stay_unique_for_duplicate_names(self):
        self._upload(["Sama,S-1,1000,1,", "Sama,S-2,1000,1,"])
        slugs = set(Product.objects.filter(store=self.store).values_list("slug", flat=True))
        self.assertEqual(slugs, {"sama", "sama-2"})
