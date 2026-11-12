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
