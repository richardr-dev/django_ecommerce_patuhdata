"""The analytics dashboard: totals, comparison, channels, locations, new vs returning, drill-down."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.channels import analytics
from apps.channels.models import ChannelBuyer, ChannelOrder, SalesChannel
from apps.orders.models import Order, OrderItem
from apps.shops.models import Customer


class AnalyticsTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("merchant-sales-analytics")
        self.channel = SalesChannel.objects.create(store=self.store, platform="tokopedia", name="Toko")

    def order(self, name, total, channel="web", days_ago=1, province="", city="", status="completed",
            customer=None, phone="", buyer=None, sku="KBL"):
        when = self.today - datetime.timedelta(days=days_ago)
        order = Order.objects.create(store=self.store, customer=customer, customer_name=name,
            phone=phone or "-", shipping_address="x", subtotal=total, total=total, channel=channel,
            province=province, city=city, status=status, idempotency_key=f"an-{name}-{days_ago}-{total}-{channel}")
        Order.objects.filter(pk=order.pk).update(created_at=datetime.datetime.combine(
            when, datetime.time(12, 0), tzinfo=datetime.UTC))
        OrderItem.objects.create(order=order, product=self.kabel if sku == "KBL" else self.lampu, name=sku,
            sku=sku, unit_price=total, quantity=1, line_total=total)
        if buyer:
            ChannelOrder.objects.create(channel=self.channel, order=order, buyer=buyer,
                external_order_id=f"X{order.pk}")
        return order

    def data(self, **params):
        from django.http import QueryDict
        q = QueryDict(mutable=True)
        for k, v in params.items():
            q.setlist(k, v if isinstance(v, list) else [v])
        return analytics.analyze(self.store, q, self.today)

    def test_totals_exclude_cancelled_and_refunded_and_count_unpaid_invoices(self):
        self.order("A", 100_000)
        self.order("B", 50_000, status="pending")          # an unpaid invoice is still a sale
        self.order("C", 70_000, status="cancelled")
        self.order("D", 30_000, status="refunded")
        a = self.data()
        self.assertEqual((a["revenue"], a["orders"]), (Decimal(150_000), 2))
        self.assertEqual(a["avg"], Decimal(75_000))
        self.assertEqual(a["cancelled"], 1)

    def test_comparison_with_the_previous_period(self):
        self.order("A", 200_000, days_ago=3)                 # this 30 days
        self.order("B", 100_000, days_ago=40)                # the 30 days before
        a = self.data()
        self.assertEqual(a["revenue"], Decimal(200_000))
        self.assertEqual(a["previous_revenue"], Decimal(100_000))
        self.assertEqual(round(a["revenue_delta"]), 100)

    def test_channels_and_the_channel_filter(self):
        self.order("A", 300_000, channel="tokopedia")
        self.order("B", 100_000, channel="web")
        a = self.data()
        self.assertEqual([(c["code"], c["share"]) for c in a["channels"]], [("tokopedia", 75.0), ("web", 25.0)])
        only = self.data(channel="tokopedia")
        self.assertEqual((only["revenue"], len(only["channels"])), (Decimal(300_000), 1))

    def test_places_come_from_the_order_else_the_marketplace_buyer(self):
        buyer = ChannelBuyer.objects.create(store=self.store, platform="tokopedia", external_id="@x",
            city="Kab. Bogor", province="Jawa Barat")
        self.order("T", 200_000, channel="tokopedia", buyer=buyer)
        self.order("W", 100_000, province="Jawa Barat", city="Bandung")
        self.order("S", 50_000, province="Jawa Timur", city="Surabaya")
        self.order("N", 10_000)                              # a shop sale: no place
        a = self.data()
        self.assertEqual([(p["name"], p["revenue"]) for p in a["provinces"]],
            [("Jawa Barat", Decimal(300_000)), ("Jawa Timur", Decimal(50_000))])
        self.assertEqual({c["name"] for c in a["cities"]}, {"Kab. Bogor", "Bandung", "Surabaya"})
        self.assertEqual(a["unknown_province"]["orders"], 1)

    def test_new_versus_returning_buyers(self):
        old = Customer.objects.create(store=self.store, name="Lama", phone="0811")
        new = Customer.objects.create(store=self.store, name="Baru", phone="0822")
        self.order("Lama", 10_000, days_ago=90, customer=old)   # bought before the period
        self.order("Lama", 20_000, days_ago=2, customer=old)
        self.order("Baru", 30_000, days_ago=2, customer=new)
        a = self.data()
        self.assertEqual((a["buyers"], a["returning"], a["new"], a["returning_pct"]), (2, 1, 1, 50))

    def test_trend_products_customers_and_weekdays(self):
        self.order("A", 100_000, days_ago=1, sku="KBL")
        self.order("A", 50_000, days_ago=2, sku="LMP")
        a = self.data()
        self.assertEqual(len(a["series"]), 30)
        self.assertEqual(a["peak"]["revenue"], Decimal(100_000))
        self.assertEqual(a["series"][-2]["pct"], 100.0)
        self.assertEqual([p["sku"] for p in a["products"]], ["KBL", "LMP"])
        self.assertEqual(a["customers"][0]["revenue"], Decimal(150_000))
        self.assertEqual(sum(d["orders"] for d in a["weekdays"]), 2)
        long = self.data(range="year")
        self.assertTrue(long["weekly"] or long["span"] <= 92)

    def test_page_and_partial_and_drilldown_links(self):
        self.order("A", 100_000, channel="tokopedia", province="Jawa Barat", city="Bogor")
        page = self.client.get(self.url)
        self.assertContains(page, "Analitik penjualan")
        self.assertContains(page, "Jawa Barat")
        self.assertContains(page, reverse("merchant-customers") + "?location=Jawa%20Barat")
        part = self.client.get(self.url, {"channel": "tokopedia", "range": "7d"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(part, "<html")
        self.assertContains(part, "Per channel")
        self.assertContains(self.client.get(reverse("merchant-dashboard")), self.url)  # in the sidebar

    def test_empty_period_says_so(self):
        self.assertContains(self.client.get(self.url), "Belum ada penjualan")

    def test_bar_widths_are_valid_css_numbers_in_the_indonesian_locale(self):
        """A decimal comma ("45,4%") is not valid CSS and would draw every bar full width."""
        from django.utils import translation
        self.order("A", 300_000, channel="tokopedia")
        self.order("B", 100_000, channel="web")
        with translation.override("id"):
            html = self.client.get(self.url).content.decode()
        self.assertIn('style="width:100.0%"', html)
        self.assertIn('style="width:33.3%"', html)
        self.assertNotRegex(html, r'width:\d+,\d+%')
        self.assertNotRegex(html, r'height:\d+,\d+%')
