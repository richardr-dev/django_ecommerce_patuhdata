"""Pesanan shows every order by default; website-only is one click, and the work-queue tabs stay the store's own."""
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

    def test_everything_is_shown_by_default_including_marketplace(self):
        names, page = self.names()
        self.assertEqual(names, {"Pembeli Web", "Pembeli Shopee", "Impor File"})
        self.assertTrue(page.context["with_marketplace"])
        self.assertNotContains(page, "tidak ditampilkan")             # nothing is hidden, so no banner

    def test_website_only_is_one_click_away_and_says_so(self):
        names, page = self.names(sumber="web")
        self.assertEqual(names, {"Pembeli Web"})
        self.assertEqual(page.context["hidden_marketplace"], 2)
        self.assertContains(page, "2 pesanan marketplace tidak ditampilkan")
        self.assertContains(page, "Tampilkan semua")

    def test_tab_counts_follow_the_filters_so_they_match_the_list(self):
        self.assertEqual(self.client.get(self.url, {"tab": "all"}).context["total"], 3)
        shopee = self.client.get(self.url, {"tab": "all", "channel": "shopee"})
        self.assertEqual(shopee.context["total"], 1)                              # the "Semua" tab says 1
        self.assertEqual(shopee.context["page_obj"].paginator.count, 1)            # and so does the list


    def test_the_work_queues_count_only_what_the_store_ships_itself(self):
        Order.objects.filter(pk=self.shopee.pk).update(status="paid")        # a marketplace order in a paid state
        Order.objects.filter(pk=self.web.pk).update(status="paid")
        tabs = {tab["key"]: tab["count"] for tab in self.client.get(self.url, {"tab": "all"}).context["tabs"]}
        self.assertEqual(tabs["pack"], 1)                                     # the website order, not the Shopee one
        queue = self.client.get(self.url, {"tab": "pack"})
        self.assertEqual({o.customer_name for o in queue.context["orders"]}, {"Pembeli Web"})

    def test_picking_a_marketplace_channel_shows_it_even_in_the_website_view(self):
        names, page = self.names(channel="shopee", sumber="web")
        self.assertEqual(names, {"Pembeli Shopee"})
        self.assertTrue(page.context["with_marketplace"])

    def test_the_channel_row_has_a_semua_chip_that_is_on_without_a_channel(self):
        html = self.client.get(self.url, {"tab": "all"}).content.decode()
        chip = html.split("data-channel-all")[1].split(">")[0]
        self.assertIn("checked", chip)
        picked = self.client.get(self.url, {"tab": "all", "channel": "shopee"}).content.decode()
        self.assertNotIn("checked", picked.split("data-channel-all")[1].split(">")[0])

    def test_marketplace_sales_still_in_the_invoice_list(self):
        page = self.client.get(reverse("merchant-sales-invoices"))
        found = {o.customer_name for o in page.context["orders"]}
        self.assertTrue({"Pembeli Shopee", "Impor File"} <= found)

    def test_kpi_tiles_follow_the_view_but_the_work_numbers_stay_the_stores_own(self):
        everything = self.client.get(self.url, {"tab": "all"})
        self.assertEqual(everything.context["kpi"]["orders"], 3)                 # today's orders on screen
        self.assertContains(everything, "Penjualan hari ini")
        Order.objects.filter(pk=self.shopee.pk).update(status="paid")
        Order.objects.filter(pk=self.web.pk).update(status="paid")
        self.assertEqual(self.client.get(self.url, {"tab": "all"}).context["kpi"]["to_ship"], 1)   # not the Shopee order
        self.assertEqual(self.client.get(self.url, {"tab": "all", "sumber": "web"}).context["kpi"]["orders"], 1)


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
