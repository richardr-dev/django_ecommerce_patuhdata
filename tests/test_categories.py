"""Sub-categories (Sepatu › Sneakers, up to 3 levels): the form refuses loops and depth, the import builds paths."""
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.categories import ensure_paths
from apps.catalog.models import Category, Product, Warehouse
from apps.orders.forms import CategoryForm
from apps.orders.setup_views import import_products, parse_import_rows
from apps.shops.models import Store


class CategoryTreeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("cat-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-cat")
        Warehouse.objects.create(store=self.store, code="MAIN", name="Gudang Utama")

    def test_a_path_creates_every_level_once_and_a_single_name_finds_it(self):
        found = ensure_paths(self.store, ["Sepatu > Sneakers", "Sepatu › Formal", "sepatu > sneakers"])
        sneakers = found["Sepatu > Sneakers"]
        self.assertEqual((sneakers.path, sneakers.root.name), ("Sepatu › Sneakers", "Sepatu"))
        self.assertEqual(found["sepatu > sneakers"], sneakers)
        self.assertEqual(Category.objects.filter(store=self.store).count(), 3)
        self.assertEqual(ensure_paths(self.store, ["Sneakers"])["Sneakers"], sneakers)
        deep = ensure_paths(self.store, ["A > B > C > D"])["A > B > C > D"]
        self.assertEqual(deep.path, "A › B › C D")                 # never deeper than 3

    def test_the_import_files_products_under_the_deepest_level(self):
        import_products(self.store, parse_import_rows([{"sku": "S1", "name": "Sneakers 41", "price": "1",
            "category": "Sepatu > Sneakers"}]))
        self.assertEqual(Product.objects.get(sku="S1").category.path, "Sepatu › Sneakers")

    def test_the_form_refuses_a_loop_and_a_fourth_level(self):
        found = ensure_paths(self.store, ["A > B > C"])
        c = found["A > B > C"]
        a = c.root
        a.parent = c
        with self.assertRaisesRegex(ValidationError, "induk dirinya"):
            a.clean()
        form = CategoryForm(data={"name": "D", "parent": c.pk}, store=self.store)
        self.assertFalse(form.is_valid())                          # C is 3 deep: not offered as a parent
        form = CategoryForm(data={"name": "Kets", "parent": a.pk}, store=self.store)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().slug, "a-kets")

    def test_the_pages_show_the_path(self):
        ensure_paths(self.store, ["Sepatu > Sneakers"])
        client = Client()
        client.force_login(self.owner)
        listing = client.get(reverse("merchant-entity-list", args=["categories"]))
        self.assertContains(listing, "Sepatu › Sneakers")
        product = client.get(reverse("merchant-entity-new", args=["products"]))
        self.assertContains(product, "Sepatu › Sneakers")
