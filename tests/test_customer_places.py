"""Find customers by where they buy and where they live."""
from django.urls import reverse
from test_sales_docs import Base

from apps.channels.models import ChannelBuyer
from apps.orders.models import Order
from apps.shops import segments
from apps.shops.models import Customer


class CustomerPlacesTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        buyer = ChannelBuyer.objects.create(store=self.store, platform="tokopedia", external_id="@dewi",
            name="", city="Kab. Bogor", province="Jawa Barat")
        self.dewi = Customer.objects.create(store=self.store, name="@dewi", channel_buyer=buyer,
            status="active")
        self.rina = Customer.objects.create(store=self.store, name="Rina", phone="0811", status="active",
            address="Jl. Melati 5, Surabaya, Jawa Timur")
        self.tono = Customer.objects.create(store=self.store, name="Tono", phone="0822", status="active")
        for customer, channel, city, province in ((self.rina, "shopee", "Surabaya", "Jawa Timur"),
                (self.tono, "web", "Bandung", "Jawa Barat")):
            Order.objects.create(store=self.store, customer=customer, customer_name=customer.name,
                phone=customer.phone, shipping_address="x", subtotal=1, total=100_000, channel=channel,
                city=city, province=province, status="completed", idempotency_key=f"pl-{customer.name}")
        self.url = reverse("merchant-customers")

    def names(self, **params):
        page = self.client.get(self.url, params)
        return {c.name for c in page.context["customers"]}

    def test_who_buys_on_tokopedia_alone(self):
        self.assertEqual(self.names(channel="tokopedia"), {"@dewi"})
        self.assertEqual(self.names(channel="shopee"), {"Rina"})
        self.assertEqual(self.names(channel=["shopee", "tokopedia"]), {"@dewi", "Rina"})
        self.assertEqual(self.names(channel="web"), {"Tono"})

    def test_find_by_city_province_or_address_words(self):
        self.assertEqual(self.names(location="bogor"), {"@dewi"})            # the marketplace buyer's city
        self.assertEqual(self.names(location="jawa barat"), {"@dewi", "Tono"})  # buyer province + order province
        self.assertEqual(self.names(location="surabaya"), {"Rina"})           # address and order
        self.assertEqual(self.names(location="melati"), {"Rina"})             # a street in the address
        self.assertEqual(self.names(location="bogor jawa barat"), {"@dewi"})  # every word must match
        self.assertEqual(self.names(location="papua"), set())

    def test_channel_and_location_combine_with_the_other_filters(self):
        self.assertEqual(self.names(channel="tokopedia", location="jawa barat"), {"@dewi"})
        self.assertEqual(self.names(channel="shopee", location="jawa barat"), set())
        self.assertEqual(self.names(channel="web", min_orders="1"), {"Tono"})

    def test_page_shows_where_they_are_and_where_they_buy(self):
        page = self.client.get(self.url, {"channel": "tokopedia"})
        self.assertContains(page, "Kab. Bogor, Jawa Barat")
        self.assertContains(page, "Tokopedia")
        self.assertEqual(page.context["provinces"], [("Jawa Barat", 1)])
        self.assertContains(page, 'name="location"')

    def test_segment_is_described_and_survives_a_broadcast_audience(self):
        segment = segments.parse({"channel": "tokopedia,bogus", "location": "bogor"})
        self.assertEqual(segment, {"location": "bogor", "channel": "tokopedia"})
        self.assertEqual(segments.describe(segment), 'beli di Tokopedia, lokasi "bogor"')
        self.dewi.accepts_marketing, self.dewi.email = True, "d@example.com"
        self.dewi.save()
        self.assertEqual(list(segments.broadcast_audience(self.store, segment)), [self.dewi])

    def test_htmx_returns_only_the_list(self):
        part = self.client.get(self.url, {"channel": "tokopedia"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(part, "<html")
        self.assertContains(part, "@dewi")
        self.assertNotContains(part, "Rina")

    def test_waiting_marketplace_buyers_are_found_by_channel_and_place(self):
        Customer.objects.filter(pk=self.dewi.pk).update(status="pending")
        self.assertEqual(self.names(), {"Rina", "Tono", "Budi"})              # plain list: approved only
        self.assertEqual(self.names(channel="tokopedia"), {"@dewi"})          # filtering by channel finds them
        self.assertEqual(self.names(location="bogor"), {"@dewi"})
        self.assertEqual(self.names(channel="tokopedia", pending="0"), set())  # unless switched off
        self.assertEqual(self.names(pending="1"), {"@dewi", "Rina", "Tono", "Budi"})
        self.assertContains(self.client.get(self.url, {"channel": "tokopedia"}), "Menunggu persetujuan")
