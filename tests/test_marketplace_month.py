"""Bulan marketplace (a shop's month as five steps) and Impor data (every import on one page)."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels import marketplace_month
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.models import SalesChannel, UnmappedSku
from apps.channels.services import import_orders
from apps.shops.models import Store, StoreStaff
from apps.tax.services import month_bounds


def _order(external_id, sku="KOPI"):
    return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812", address="Bandung",
        status_text="Selesai", lines=[OrderLine(sku=sku, quantity=1, unit_price=Decimal(15000))])


class MarketplaceMonthTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("mm-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-mm")
        kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=15000,
            cost_price=Decimal(9000))
        InventoryLevel.objects.create(warehouse=Warehouse.objects.create(store=self.store, name="G", code="G1"),
            product=kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Toko Shopee")
        today = timezone.localdate()
        self.start, self.end = month_bounds(today.year, today.month)
        self.client = Client()
        self.client.force_login(self.owner)

    def steps(self):
        shop = marketplace_month.build(self.store, self.start, self.end)["shops"][0]
        return {step["key"]: step["state"] for step in shop["steps"]}

    def test_a_shop_with_nothing_imported_starts_at_step_one(self):
        self.assertEqual(self.steps()["orders"], "todo")

    def test_imported_orders_tick_orders_and_invoices_and_ask_for_the_income_report(self):
        import_orders(self.channel, [_order("SHP-1"), _order("SHP-2")], finished_only=True, states={"completed"})
        state = self.steps()
        self.assertEqual((state["orders"], state["invoices"], state["income"], state["check"]),
            ("ok", "ok", "warn", "ok"))

    def test_an_unmapped_sku_is_a_difference_to_fix(self):
        import_orders(self.channel, [_order("SHP-3")], finished_only=True, states={"completed"})
        UnmappedSku.objects.create(store=self.store, channel=self.channel, sku="X-1", name="Barang baru")
        self.assertEqual(self.steps()["check"], "warn")

    def test_the_pages_open(self):
        import_orders(self.channel, [_order("SHP-4")], finished_only=True, states={"completed"})
        page = self.client.get(reverse("merchant-marketplace-month"))
        self.assertContains(page, "Toko Shopee")
        self.assertContains(page, "Impor penghasilan")
        hub = self.client.get(reverse("merchant-import-hub"))
        for title in ("Penjualan dari Excel", "Pesanan marketplace", "Penghasilan marketplace", "Produk",
                "Pelanggan", "Penerimaan pembayaran", "Saldo awal piutang"):
            self.assertContains(hub, title)
        self.assertContains(hub, "Toko Shopee")                  # the last marketplace import

    def test_the_hub_shows_only_the_imports_a_person_may_use(self):
        person = User.objects.create_user("mm-wh")
        StoreStaff.objects.create(store=self.store, user=person, role="ops")
        self.client.force_login(person)
        hub = self.client.get(reverse("merchant-import-hub"))
        self.assertContains(hub, "Pesanan marketplace")
        self.assertNotContains(hub, "Saldo awal utang")
