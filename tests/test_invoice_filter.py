"""The sales invoice filter: one implementation for the page and its exports."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.channels import invoice_filters
from apps.orders.models import Order


class InvoiceFilterTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.a = self.invoice(kabel=2, days_ago=0)                      # Budi, today
        self.b = self.invoice(kabel=0, lampu=3, days_ago=40, crm=False)  # old, no CRM
        Order.objects.filter(pk=self.a.order_id).update(phone="081234567890", channel="shopee")
        Order.objects.filter(pk=self.b.order_id).update(phone="0899-111-222", channel="tokopedia",
            customer_name="Siti Aminah")
        self.url = reverse("merchant-sales-invoices")

    def numbers(self, **params):
        page = self.client.get(self.url, params)
        return {o.invoice.number for o in page.context["orders"] if getattr(o, "invoice", None)}

    def test_search_by_name_phone_and_product(self):
        self.assertEqual(self.numbers(q="Siti"), {self.b.number})
        self.assertEqual(self.numbers(q="081234567890"), {self.a.number})
        self.assertEqual(self.numbers(q="+62 812 3456 7890"), {self.a.number})  # same phone, other format
        self.assertEqual(self.numbers(q="LMP"), {self.b.number})                 # product SKU
        self.assertEqual(self.numbers(q="Kabel"), {self.a.number})                # product name

    def test_product_search_does_not_duplicate_rows_or_break_totals(self):
        page = self.client.get(self.url, {"q": "K"})
        ids = [o.pk for o in page.context["orders"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_period_presets_and_custom_range(self):
        self.assertEqual(self.numbers(range="today"), {self.a.number})
        self.assertEqual(self.numbers(range="30d"), {self.a.number})
        old = (self.today - datetime.timedelta(days=45)).isoformat()
        mid = (self.today - datetime.timedelta(days=35)).isoformat()
        self.assertEqual(self.numbers(date_from=old, date_to=mid), {self.b.number})
        # a custom range wins over a preset
        self.assertEqual(self.numbers(range="today", date_from=old, date_to=mid), {self.b.number})

    def test_channel_status_tab_and_amount(self):
        self.assertEqual(self.numbers(channel="tokopedia"), {self.b.number})
        self.assertEqual(self.numbers(inv="awaiting"), {self.a.number, self.b.number})
        self.assertEqual(self.numbers(inv="paid"), set())
        self.assertEqual(self.numbers(min_total="200000"), {self.a.number})
        self.assertEqual(self.numbers(max_total="200000"), {self.b.number})
        self.assertEqual(self.numbers(min_total="1.000"), {self.a.number, self.b.number})

    def test_active_filters_are_listed_with_what_to_clear(self):
        page = self.client.get(self.url, {"q": "Siti", "channel": "tokopedia", "range": "30d"})
        labels = [f["label"] for f in page.context["active_filters"]]
        self.assertEqual(len(labels), 3)
        self.assertTrue(any("Siti" in label for label in labels))
        self.assertTrue(any(f["keys"][0] == "range" and "date_from" in f["keys"]
            for f in page.context["active_filters"]))

    def test_htmx_request_returns_only_the_list_and_oob_tabs(self):
        full = self.client.get(self.url)
        part = self.client.get(self.url, {"q": "Siti"}, HTTP_HX_REQUEST="true")
        self.assertContains(full, 'id="inv-filter"')
        self.assertNotContains(part, "<html")
        self.assertContains(part, 'hx-swap-oob="true"')
        self.assertContains(part, self.b.number)
        self.assertNotContains(part, self.a.number)
        self.assertIn("HX-Request", part["Vary"])

    def test_exports_use_the_same_filter(self):
        csv = self.client.get(reverse("merchant-invoices-csv"), {"q": "Siti"}).content.decode()
        self.assertIn("Siti Aminah", csv)
        self.assertNotIn("Budi", csv)
        resolved = invoice_filters.apply_filters(Order.objects.filter(store=self.store),
            {"channel": "shopee"})[0]
        self.assertEqual([o.pk for o in resolved], [self.a.order_id])

    def test_totals_follow_the_filter(self):
        page = self.client.get(self.url, {"channel": "tokopedia"})
        self.assertEqual(page.context["outstanding"], Decimal(3 * 50_000 + 16_500))


class FlexibleFilterTests(InvoiceFilterTests):
    def test_several_channels_at_once(self):
        page = self.client.get(self.url, {"channel": ["shopee", "tokopedia"]})
        numbers = {o.invoice.number for o in page.context["orders"] if getattr(o, "invoice", None)}
        self.assertEqual(numbers, {self.a.number, self.b.number})
        self.assertEqual(self.numbers(channel="shopee,tokopedia"), numbers)
        labels = [f["label"] for f in page.context["active_filters"]]
        self.assertIn("Shopee + Tokopedia", labels[0])

    def test_sort_orders(self):
        def first(sort):
            page = self.client.get(self.url, {"sort": sort})
            return page.context["orders"][0].invoice.number
        self.assertEqual(first("newest"), self.a.number)
        self.assertEqual(first("oldest"), self.b.number)
        self.assertEqual(first("total_desc"), self.a.number)
        self.assertEqual(first("total_asc"), self.b.number)

    def test_crm_filter(self):
        self.assertEqual(self.numbers(crm="yes"), {self.a.number})
        self.assertEqual(self.numbers(crm="no"), {self.b.number})

    def test_mitra_filter_shows_only_that_partners_invoices(self):
        from apps.settlements.models import ConsignmentPartner
        ab = ConsignmentPartner.objects.create(store=self.store, name="Mitra Jaya")
        other = ConsignmentPartner.objects.create(store=self.store, name="Mitra Lain")
        Order.objects.filter(pk=self.a.order_id).update(consignment_partner=ab)
        Order.objects.filter(pk=self.b.order_id).update(consignment_partner=other)
        self.assertEqual(self.numbers(mitra=ab.pk), {self.a.number})
        self.assertEqual(self.numbers(mitra=[ab.pk, other.pk]), {self.a.number, self.b.number})
        page = self.client.get(self.url, {"mitra": ab.pk})
        self.assertEqual({c["label"] for c in page.context["mitra_chips"]}, {"Mitra Jaya", "Mitra Lain"})
        self.assertIn(f"mitra={ab.pk}", page.context["filter_query"])
        self.assertContains(page, "Mitra: Mitra Jaya")

    def test_export_keeps_every_channel(self):
        page = self.client.get(self.url, {"channel": ["shopee", "tokopedia"], "q": "a, b"})
        self.assertIn("channel=shopee", page.context["filter_query"])
        self.assertIn("channel=tokopedia", page.context["filter_query"])
        self.assertEqual(page.context["filter_query"].count("q="), 1)


class CustomerHistoryTests(InvoiceFilterTests):
    def test_history_filters_and_shows_what_was_bought(self):
        url = reverse("merchant-customer-detail", args=[self.customer.pk])
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertContains(full, "Apa saja yang dibeli")
        self.assertEqual(full.context["history_count"], 1)
        self.assertEqual([r["sku"] for r in full.context["purchased"]], ["KBL"])
        none = self.client.get(url, {"q": "LMP"})
        self.assertEqual(none.context["history_count"], 0)
        part = self.client.get(url, {"range": "30d"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(part, "<html")
        self.assertContains(part, "Total belanja")


class OrderPageFilterTests(InvoiceFilterTests):
    def ids(self, **params):
        page = self.client.get(reverse("merchant-orders"), {"tab": "all", "sumber": "semua", **params})
        return {o.pk for o in page.context["orders"]}

    def test_same_search_and_period_as_the_invoice_list(self):
        a, b = self.a.order_id, self.b.order_id
        self.assertEqual(self.ids(q="LMP"), {b})
        self.assertEqual(self.ids(q="+62 812 3456 7890"), {a})
        self.assertEqual(self.ids(range="today"), {a})
        self.assertEqual(self.ids(channel=["shopee", "tokopedia"]), {a, b})
        self.assertEqual(self.ids(min_total="200000"), {a})

    def test_tracking_number_and_short_id_still_work(self):
        Order.objects.filter(pk=self.a.order_id).update(tracking_number="JNE123456")
        self.assertEqual(self.ids(q="JNE123456"), {self.a.order_id})
        self.assertEqual(self.ids(q=str(self.b.order_id)[:8]), {self.b.order_id})

    def test_sort_and_live_partial(self):
        page = self.client.get(reverse("merchant-orders"), {"tab": "all", "sort": "oldest", "sumber": "semua"})
        self.assertEqual(page.context["orders"][0].pk, self.b.order_id)
        part = self.client.get(reverse("merchant-orders"), {"tab": "all", "q": "Siti", "sumber": "semua"},
            HTTP_HX_REQUEST="true")
        self.assertNotContains(part, "<html")
        self.assertContains(part, "Siti Aminah")
        self.assertContains(part, "Filter aktif")
        full = self.client.get(reverse("merchant-orders"), {"tab": "all"})
        self.assertContains(full, 'id="ord-filter"')
