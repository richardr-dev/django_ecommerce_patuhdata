"""Pesanan is the website's queue: marketplace imports live in Invoice penjualan, not twice."""
from django.urls import reverse
from test_sales_docs import Base

from apps.channels.models import ChannelOrder, SalesChannel
from apps.orders.models import Order
from apps.orders.scope import marketplace_orders, website_orders


class OrdersScopeTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.web = self.invoice(kabel=1).order
        Order.objects.filter(pk=self.web.pk).update(channel="web", customer_name="Pembeli Web")
        self.shopee = self.invoice(kabel=1).order
        Order.objects.filter(pk=self.shopee.pk).update(channel="shopee", customer_name="Pembeli Shopee")
        self.imported = self.invoice(kabel=1).order  # imported file, channel not even set right
        Order.objects.filter(pk=self.imported.pk).update(channel="manual", customer_name="Impor File")
        channel = SalesChannel.objects.create(store=self.store, platform="tokopedia", name="Toko")
        ChannelOrder.objects.create(channel=channel, order=self.imported, external_order_id="TKP-1")
        self.url = reverse("merchant-orders")

    def names(self, **params):
        page = self.client.get(self.url, {"tab": "all", **params})
        return {o.customer_name for o in page.context["orders"]}, page

    def test_scope_helpers(self):
        self.assertEqual(set(website_orders(self.store).values_list("pk", flat=True)), {self.web.pk})
        self.assertEqual(set(marketplace_orders(self.store).values_list("pk", flat=True)),
            {self.shopee.pk, self.imported.pk})

    def test_marketplace_orders_are_hidden_by_default(self):
        names, page = self.names()
        self.assertEqual(names, {"Pembeli Web"})
        self.assertEqual(page.context["hidden_marketplace"], 2)
        self.assertContains(page, "2 pesanan marketplace tidak ditampilkan")

    def test_tabs_and_counts_ignore_marketplace(self):
        page = self.client.get(self.url, {"tab": "all"})
        self.assertEqual(page.context["total"], 1)

    def test_one_switch_shows_them(self):
        names, _ = self.names(sumber="semua")
        self.assertEqual(names, {"Pembeli Web", "Pembeli Shopee", "Impor File"})

    def test_picking_a_marketplace_channel_shows_it_without_the_switch(self):
        names, page = self.names(channel="shopee")
        self.assertEqual(names, {"Pembeli Shopee"})
        self.assertTrue(page.context["with_marketplace"])

    def test_marketplace_sales_still_in_the_invoice_list(self):
        page = self.client.get(reverse("merchant-sales-invoices"))
        found = {o.customer_name for o in page.context["orders"]}
        self.assertTrue({"Pembeli Shopee", "Impor File"} <= found)

    def test_kpi_tiles_count_website_orders_only(self):
        page = self.client.get(self.url, {"tab": "all"})
        self.assertEqual(page.context["kpi"]["orders"], 1)
        self.assertContains(page, "Penjualan hari ini")


class CleanupTests(Base):
    def test_empty_batches_are_found_and_removed_but_used_ones_stay(self):
        from apps.channels import cleanup
        from apps.channels.models import ImportBatch
        channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Toko")
        empty = ImportBatch.objects.create(store=self.store, channel=channel, file_name="a.xlsx")
        used = ImportBatch.objects.create(store=self.store, channel=channel, file_name="b.xlsx")
        order = self.invoice().order
        ChannelOrder.objects.create(channel=channel, order=order, external_order_id="X", batch=used)
        self.assertEqual(list(cleanup.empty_batches(self.store)), [empty])
        report = cleanup.clean(self.store)
        self.assertEqual((report["empty_batches"], report["removed"]), (1, 0))  # dry run
        self.assertTrue(ImportBatch.objects.filter(pk=empty.pk).exists())
        cleanup.clean(self.store, apply=True)
        self.assertFalse(ImportBatch.objects.filter(pk=empty.pk).exists())
        self.assertTrue(ImportBatch.objects.filter(pk=used.pk).exists())
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_duplicates_and_look_alikes_are_reported_not_deleted(self):
        from apps.channels import cleanup
        for key in ("k1", "k2"):
            Order.objects.create(store=self.store, customer_name="Dewi", phone="08", shipping_address="-",
                subtotal=50_000, total=50_000, channel="shopee", external_order_id="SHP-9",
                status="paid", idempotency_key=key)
        report = cleanup.clean(self.store, apply=True)
        self.assertEqual(report["duplicates"][0]["count"], 2)
        self.assertEqual(report["look_alikes"][0]["name"], "Dewi")
        self.assertEqual(Order.objects.filter(external_order_id="SHP-9").count(), 2)

    def test_command_runs(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("clean_orders", "--store", self.store.slug, stdout=out)
        self.assertIn("empty import batches", out.getvalue())
