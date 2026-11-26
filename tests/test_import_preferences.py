"""Each person chooses, per import, which marketplace statuses come in and whether those orders also show on
Pesanan, and may save the choice as their own default. Pesanan opens on the person's saved view."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels import import_prefs
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.models import ImportPreference, SalesChannel
from apps.channels.services import import_orders
from apps.orders.models import Order
from apps.shops.models import Store, StoreStaff


def _order(external_id, status="Selesai"):
    return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812", address="Bandung",
        status_text=status, lines=[OrderLine(sku="KOPI", quantity=1, unit_price=Decimal(15000))])


class ImportPreferenceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pref-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-pref")
        kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=15000,
            cost_price=Decimal(9000))
        InventoryLevel.objects.create(warehouse=Warehouse.objects.create(store=self.store, name="G", code="G1"),
            product=kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Toko Shopee")
        Order.objects.create(store=self.store, customer_name="Web", phone="08", shipping_address="x", channel="web",
            subtotal=10000, total=10000)
        self.client = Client()
        self.client.force_login(self.owner)

    def listed(self, query=""):
        page = self.client.get(reverse("merchant-orders") + "?tab=all" + query)
        return page, {o.customer_name for o in page.context["orders"]}

    def test_an_import_kept_out_of_pesanan_stays_in_the_books_and_shows_on_request(self):
        import_orders(self.channel, [_order("SHP-1")], finished_only=True, states={"completed"},
            show_in_orders=False)
        import_orders(self.channel, [_order("SHP-2")], finished_only=True, states={"completed"})
        page = self.listed()[0]
        self.assertEqual(len(page.context["orders"]), 2)          # the web order and SHP-2
        self.assertEqual(page.context["hidden_imports"], 1)
        self.assertContains(page, "Tampilkan juga")
        self.assertEqual(len(self.listed("&sumber=semua")[0].context["orders"]), 3)
        from apps.tax.models import Invoice
        self.assertTrue(Invoice.objects.filter(order__external_order_id="SHP-1").exists())   # still a sale

    def test_the_import_page_starts_on_the_saved_defaults(self):
        url = reverse("merchant-import") + "?platform=shopee"
        page = self.client.get(url)
        self.assertEqual(page.context["default_states"], {"completed", "cancelled"})   # built-in
        self.assertTrue(page.context["chosen_show"])
        import_prefs.save_import_defaults(self.owner, self.store, states={"completed", "shipped"},
            category="flash_sale", soft=True, show_in_orders=False)
        page = self.client.get(url)
        self.assertEqual(page.context["default_states"], {"completed", "shipped"})
        self.assertEqual((page.context["chosen_category"], page.context["chosen_soft"], page.context["chosen_show"]),
            ("flash_sale", True, False))
        self.assertContains(page, "bawaan Anda")
        other = User.objects.create_user("pref-staff")
        StoreStaff.objects.create(store=self.store, user=other, role="manager")
        self.assertEqual(import_prefs.states_of(import_prefs.get(other, self.store)), {"completed", "cancelled"})

    def test_ticking_save_stores_the_choice_even_without_a_file(self):
        self.client.post(reverse("merchant-import") + "?platform=shopee", {"platform": "shopee",
            "states": ["completed", "active"], "category": "campaign", "show_in_orders": "0", "save_default": "1"})
        pref = ImportPreference.objects.get(user=self.owner, store=self.store)
        self.assertEqual((pref.states, pref.category, pref.show_in_orders, pref.soft),
            (["completed", "active"], "campaign", False, False))

    def test_pesanan_opens_on_the_saved_view(self):
        import_orders(self.channel, [_order("SHP-3")], finished_only=True, states={"completed"})
        self.assertEqual(len(self.listed()[0].context["orders"]), 2)
        page = self.listed("&sumber=web")[0]
        self.assertContains(page, "Jadikan tampilan bawaan saya")
        self.client.post(reverse("merchant-orders-view"), {"view": "web"})
        page, names = self.listed()
        self.assertEqual(names, {"Web"})
        self.assertNotContains(page, "Jadikan tampilan bawaan saya")
        self.assertEqual(len(self.listed("&sumber=all")[0].context["orders"]), 2)
