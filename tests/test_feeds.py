"""Product feed for Google Shopping and Meta, and the Feed & QR katalog page."""
from xml.etree import ElementTree

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.shops.models import CampaignLink, Store

G = "{http://base.google.com/ns/1.0}"


@override_settings(PLATFORM_DOMAIN="")
class FeedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma & Co", slug="ruuma")
        warehouse = Warehouse.objects.create(store=self.store, name="G", code="G")
        kopi = Product.objects.create(store=self.store, name="Kopi <Gayo>", slug="kopi", sku="KOPI",
            price=50000, compare_at_price=60000, description="<p>Arabika</p>",
            image_url="https://cdn.example.com/kopi.jpg")
        InventoryLevel.objects.create(warehouse=warehouse, product=kopi, on_hand=3)
        kaos = Product.objects.create(store=self.store, name="Kaos", slug="kaos", sku="KAOS", price=100000)
        ProductVariant.objects.create(product=kaos, name="M", sku="KAOS-M", stock=0)
        ProductVariant.objects.create(product=kaos, name="L", sku="KAOS-L", stock=2, price=110000)
        Product.objects.create(store=self.store, name="Hidden", slug="x", sku="X", price=1, is_active=False)

    def test_feed_lists_products_and_variants_with_price_and_stock(self):
        reply = Client().get("/feeds/products.xml")
        self.assertIn("application/rss+xml", reply["Content-Type"])
        items = {item.find(f"{G}id").text: item for item in
            ElementTree.fromstring(reply.content).iter("item")}
        self.assertEqual(set(items), {"KOPI", "KAOS-M", "KAOS-L"})
        kopi = items["KOPI"]
        self.assertEqual(kopi.find(f"{G}title").text, "Kopi <Gayo>")  # escaped, parsed back
        self.assertEqual(kopi.find(f"{G}price").text, "60000.00 IDR")
        self.assertEqual(kopi.find(f"{G}sale_price").text, "50000.00 IDR")
        self.assertEqual(kopi.find(f"{G}availability").text, "in_stock")
        self.assertEqual(kopi.find(f"{G}description").text, "Arabika")
        self.assertEqual(items["KAOS-M"].find(f"{G}availability").text, "out_of_stock")
        self.assertEqual(items["KAOS-L"].find(f"{G}price").text, "110000.00 IDR")
        self.assertEqual(items["KAOS-L"].find(f"{G}item_group_id").text, "KAOS")
        self.assertEqual(kopi.find(f"{G}brand").text, "Ruuma & Co")

    def test_feeds_and_qr_page(self):
        CampaignLink.objects.create(store=self.store, name="QR etalase", code="etalase")
        seller = Client()
        seller.force_login(self.owner)
        page = seller.get("/merchant/feeds/")
        self.assertContains(page, "/feeds/products.xml")
        self.assertContains(page, "Link kampanye: QR etalase")
        self.assertContains(page, "/s/etalase")
