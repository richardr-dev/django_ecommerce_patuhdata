"""Marketplace channels: one Lakuin stock, orders imported from marketplaces, stock pushed out."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from apps.accounting.ledger import MARKETPLACE, post_pending_entries
from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.channels.adapters import (
    PLATFORMS,
    ChannelAdapter,
    MarketplaceOrder,
    OrderLine,
    Platform,
)
from apps.channels.models import ChannelListing, ChannelOrder, SalesChannel
from apps.channels.services import (
    ChannelImportError,
    import_order,
    match_listings,
    parse_orders_csv,
    pull_channel_orders,
    push_stock_updates,
)
from apps.orders.models import Order, PaymentTransaction
from apps.shops.models import Store

CSV = """order_id,buyer_name,phone,address,sku,quantity,unit_price,shipping_cost,discount,courier,tracking_number
SHP-1,Budi,0812,Bandung,KOPI,2,Rp 15.000,12000,5000,J&T,JT1
SHP-1,,,,TEH-M,1,20000,,,,
SHP-2,Sari,0813,Depok,KOPI,1,15000,0,0,,
"""


class FakeAdapter(ChannelAdapter):
    orders, pushed = [], []

    def fetch_orders(self, since):
        return list(self.orders)

    def push_stock(self, listing, quantity):
        self.pushed.append((listing.sku, quantity))


def _order(external_id="SHP-9", sku="KOPI", quantity=1):
    return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812",
        address="Bandung", lines=[OrderLine(sku=sku, quantity=quantity, unit_price=Decimal(15000))])


class ChannelTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=15000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=5)
        self.teh = Product.objects.create(store=self.store, name="Teh", slug="teh", sku="TEH",
            price=20000)
        self.teh_m = ProductVariant.objects.create(product=self.teh, name="M", sku="TEH-M", stock=3)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee",
            name="Toko Shopee")


class ImportTests(ChannelTestCase):
    def test_import_creates_paid_order_and_reserves_stock(self):
        order, created = import_order(self.channel, _order(quantity=2))

        self.level.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.total, Decimal(30000))
        self.assertEqual(order.email, "")
        self.assertEqual(self.level.reserved, 2)

    def test_reimport_is_a_no_op(self):
        import_order(self.channel, _order())
        _, created = import_order(self.channel, _order())

        self.assertFalse(created)
        self.assertEqual(Order.objects.count(), 1)

    def test_short_stock_saves_nothing(self):
        with self.assertRaisesMessage(ChannelImportError, "tersedia 5"):
            import_order(self.channel, _order(quantity=6))

        self.level.refresh_from_db()
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.level.reserved, 0)

    def test_variant_sku_reserves_the_variant(self):
        import_order(self.channel, _order(sku="TEH-M", quantity=2))

        self.teh_m.refresh_from_db()
        self.assertEqual(self.teh_m.reserved, 2)

    def test_parent_sku_of_a_product_with_variants_is_rejected(self):
        with self.assertRaisesMessage(ChannelImportError, "SKU varian"):
            import_order(self.channel, _order(sku="TEH"))

    def test_unknown_sku_is_rejected(self):
        with self.assertRaisesMessage(ChannelImportError, "tidak ada di katalog"):
            import_order(self.channel, _order(sku="NOPE"))

    def test_marketplace_sale_is_booked_as_marketplace_receivable(self):
        import_order(self.channel, _order())
        post_pending_entries()

        payment = PaymentTransaction.objects.get(kind="payment")
        self.assertEqual(payment.provider, "shopee")
        self.assertEqual(payment.journal_entry.lines.get(debit__gt=0).account.code, MARKETPLACE)


class CsvTests(ChannelTestCase):
    def test_rows_are_grouped_per_order_with_rupiah_amounts(self):
        first, second = parse_orders_csv(CSV)

        self.assertEqual(first.external_id, "SHP-1")
        self.assertEqual([(line.sku, line.quantity) for line in first.lines], [("KOPI", 2), ("TEH-M", 1)])
        self.assertEqual(first.lines[0].unit_price, Decimal(15000))
        self.assertEqual(first.total, Decimal(30000 + 20000 - 5000 + 12000))
        self.assertEqual(second.buyer_name, "Sari")

    def test_missing_columns_are_reported(self):
        with self.assertRaisesMessage(ChannelImportError, "unit_price"):
            parse_orders_csv("order_id,sku,quantity\nA,KOPI,1\n")


class ListingAndSyncTests(ChannelTestCase):
    def test_match_lists_each_sellable_sku_once(self):
        self.assertEqual(match_listings(self.channel), 2)
        self.assertEqual(match_listings(self.channel), 0)
        self.assertEqual(set(self.channel.listings.values_list("sku", flat=True)), {"KOPI", "TEH-M"})

    def test_stock_push_sends_only_changes(self):
        self.channel.connection = "api"
        self.channel.save()
        match_listings(self.channel)
        FakeAdapter.pushed = []
        with patch.dict(PLATFORMS, {"shopee": Platform("Shopee", FakeAdapter)}):
            push_stock_updates()
            push_stock_updates()
            self.level.on_hand = 4
            self.level.save()
            push_stock_updates()

        self.assertEqual(sorted(FakeAdapter.pushed), [("KOPI", 4), ("KOPI", 5), ("TEH-M", 3)])

    def test_unapproved_api_is_reported_not_crashed(self):
        self.channel.connection = "api"
        self.channel.save()
        match_listings(self.channel)

        push_stock_updates()
        pull_channel_orders()

        self.channel.refresh_from_db()
        self.assertIn("menunggu persetujuan", self.channel.last_error)
        self.assertIn("menunggu persetujuan", ChannelListing.objects.first().sync_error)

    def test_order_pull_cursor_waits_for_a_clean_batch(self):
        self.channel.connection = "api"
        self.channel.save()
        FakeAdapter.orders = [_order("A"), _order("B", quantity=99)]
        with patch.dict(PLATFORMS, {"shopee": Platform("Shopee", FakeAdapter)}):
            self.assertEqual(pull_channel_orders(), 1)
            self.channel.refresh_from_db()
            self.assertIsNone(self.channel.last_order_sync_at)

            FakeAdapter.orders = [_order("A")]
            pull_channel_orders()
        self.channel.refresh_from_db()
        self.assertGreater(self.channel.last_order_sync_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(ChannelOrder.objects.count(), 1)


class ChannelPageTests(ChannelTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.owner)

    def test_connect_channel_and_import_csv(self):
        response = self.client.post("/merchant/marketplace/", {"platform": "tokopedia",
            "name": "Toko Tokopedia"})
        channel = SalesChannel.objects.get(platform="tokopedia")
        self.assertRedirects(response, f"/merchant/marketplace/{channel.id}/")

        page = self.client.post(f"/merchant/marketplace/{channel.id}/", {"action": "import",
            "file": SimpleUploadedFile("orders.csv", CSV.encode(), content_type="text/csv")})
        self.assertEqual(page.context["summary"]["created"], 2)
        self.assertEqual(channel.orders.count(), 2)

    def test_stock_csv_and_template_download(self):
        match_listings(self.channel)

        stock = self.client.get(f"/merchant/marketplace/{self.channel.id}/stock.csv")
        template = self.client.get("/merchant/marketplace/template.csv")
        self.assertIn("KOPI,Kopi,,5", stock.content.decode())
        self.assertTrue(template.content.decode().startswith("order_id,buyer_name"))

    def test_other_sellers_cannot_open_the_channel(self):
        stranger = User.objects.create_user("stranger")
        Store.objects.create(owner=stranger, name="Lain", slug="lain")
        client = Client()
        client.force_login(stranger)

        self.assertEqual(client.get(f"/merchant/marketplace/{self.channel.id}/").status_code, 404)
