"""Marketplace SKUs map to the catalog, bundles expand into their parts, unknown SKUs queue up."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.mapping import MappingError, map_sku, resolve_unmapped, save_bundle
from apps.channels.models import ChannelSku, SalesChannel, UnmappedSku
from apps.channels.services import ChannelImportError, import_order
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store


def _order(sku, quantity=1, price=50000, name="", external_id="X-1"):
    return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812",
        address="Bandung", lines=[OrderLine(sku=sku, quantity=quantity,
            unit_price=Decimal(price), name=name)])


class Base(TestCase):
    def setUp(self):
        owner = User.objects.create_user("mapper")
        self.store = Store.objects.create(owner=owner, name="Toko", slug="toko-map")
        gudang = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=30000)
        self.kopi_level = InventoryLevel.objects.create(warehouse=gudang, product=self.kopi,
            on_hand=20)
        self.teh = Product.objects.create(store=self.store, name="Teh", slug="teh", sku="TEH",
            price=10000)
        self.teh_level = InventoryLevel.objects.create(warehouse=gudang, product=self.teh,
            on_hand=20)
        self.channel = SalesChannel.objects.create(store=self.store, platform="tiktok_shop",
            name="TikTok Toko")


class MappingTests(Base):
    def test_a_mapped_sku_imports_as_the_master_item(self):
        map_sku(self.channel, "TT-KOPI-250", product=self.kopi)

        order, created = import_order(self.channel, _order("TT-KOPI-250", quantity=2))

        self.assertTrue(created)
        self.assertEqual(order.items.get().product, self.kopi)
        self.kopi_level.refresh_from_db()
        self.assertEqual(self.kopi_level.on_hand, 18)

    def test_unknown_sku_is_refused_and_queued_once_per_sku(self):
        for external_id in ("A", "B"):
            with self.assertRaises(ChannelImportError):
                import_order(self.channel, _order("NEW-1", name="Kopi baru",
                    external_id=external_id))

        row = UnmappedSku.objects.get()
        self.assertEqual((row.sku, row.times_seen, row.resolved), ("NEW-1", 2, False))

    def test_resolving_the_queue_maps_the_sku_for_next_time(self):
        with self.assertRaises(ChannelImportError):
            import_order(self.channel, _order("NEW-1"))

        resolve_unmapped(UnmappedSku.objects.get(), product=self.kopi)
        order, _ = import_order(self.channel, _order("NEW-1"))

        self.assertEqual(order.items.get().product, self.kopi)
        self.assertTrue(UnmappedSku.objects.get().resolved)

    def test_historical_placeholder_moves_onto_the_real_item_when_mapped(self):
        order, _ = import_order(self.channel, _order("OLD-1", name="Kopi lama"), soft=True)
        placeholder = order.items.get().product
        self.assertNotEqual(placeholder, self.kopi)

        _, moved = resolve_unmapped(UnmappedSku.objects.get(), product=self.kopi)

        self.assertEqual(moved, 1)
        self.assertEqual(OrderItem.objects.get().product, self.kopi)
        placeholder.refresh_from_db()
        self.assertFalse(placeholder.is_active)


class BundleTests(Base):
    def setUp(self):
        super().setUp()
        self.bundle = save_bundle(self.store, sku="PAKET-1", name="Paket Kopi Teh",
            components=[(self.kopi, None, 2), (self.teh, None, 1)])

    def test_bundle_sale_takes_every_component_and_shares_the_price(self):
        # Components list at 2 × 30.000 and 1 × 10.000: a 70.000 sale splits 60.000 / 10.000.
        order, _ = import_order(self.channel, _order("PAKET-1", quantity=1, price=70000))

        items = {item.sku: item for item in order.items.all()}
        self.assertEqual((items["KOPI"].quantity, items["KOPI"].line_total), (2, Decimal(60000)))
        self.assertEqual((items["TEH"].quantity, items["TEH"].line_total), (1, Decimal(10000)))
        self.assertEqual({item.bundle_sku for item in items.values()}, {"PAKET-1"})
        self.assertEqual(order.total, Decimal(70000))
        self.kopi_level.refresh_from_db()
        self.teh_level.refresh_from_db()
        self.assertEqual((self.kopi_level.on_hand, self.teh_level.on_hand), (18, 19))

    def test_shares_add_up_exactly_when_the_split_does_not_divide(self):
        order, _ = import_order(self.channel, _order("PAKET-1", quantity=3, price=33333))

        self.assertEqual(sum(item.line_total for item in order.items.all()),
            Decimal(33333) * 3)

    def test_a_channel_sku_can_point_at_a_bundle(self):
        map_sku(self.channel, "TT-PAKET", bundle=self.bundle)

        order, _ = import_order(self.channel, _order("TT-PAKET", quantity=2, price=70000))

        self.assertEqual(order.items.count(), 2)
        self.kopi_level.refresh_from_db()
        self.assertEqual(self.kopi_level.on_hand, 16)

    def test_bundle_availability_is_limited_by_the_scarcest_part(self):
        self.teh_level.on_hand = 3
        self.teh_level.save()

        self.assertEqual(self.bundle.available_stock, 3)

    def test_bundle_sku_cannot_clash_with_a_product(self):
        with self.assertRaises(MappingError):
            save_bundle(self.store, sku="KOPI", name="Salah", components=[(self.kopi, None, 1)])
        with self.assertRaises(MappingError):
            save_bundle(self.store, sku="PAKET-2", name="Kosong", components=[])

    def test_cancelling_a_bundle_order_returns_every_component(self):
        import_order(self.channel, _order("PAKET-1", price=70000))
        cancelled = _order("PAKET-1", price=70000)
        cancelled = MarketplaceOrder(**{**cancelled.__dict__, "status_text": "Batal"})

        import_order(self.channel, cancelled)

        self.kopi_level.refresh_from_db()
        self.teh_level.refresh_from_db()
        self.assertEqual((self.kopi_level.on_hand, self.teh_level.on_hand), (20, 20))
        self.assertEqual(Order.objects.get().status, "cancelled")


class PageTests(Base):
    def test_pages_load_and_map(self):
        self.client.force_login(self.store.owner)
        base = f"?store={self.store.id}"
        with self.assertRaises(ChannelImportError):
            import_order(self.channel, _order("NEW-9"))

        page = self.client.get(f"/merchant/marketplace/sku/{base}")
        self.assertContains(page, "NEW-9")
        self.client.post(f"/merchant/marketplace/sku/{base}", {
            "unmapped": UnmappedSku.objects.get().id, "target": "KOPI"})
        self.assertEqual(ChannelSku.objects.get().product, self.kopi)

        self.client.post(f"/merchant/paket/{base}", {"sku": "P-9", "name": "Paket",
            "components": "KOPI x 2\nTEH"})
        self.assertContains(self.client.get(f"/merchant/paket/{base}"), "Paket")
