"""The all-channel sales report adds up the same orders the stock and ledger use."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels.reporting import sales_report
from apps.orders.models import Order, OrderItem
from apps.settlements.models import ConsignmentPartner
from apps.shops.models import Customer, Store


class OverviewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("report-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-laporan")
        gudang = Warehouse.objects.create(store=self.store, name="G", code="G1")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000, cost_price=Decimal(20000), ab_shop_price=Decimal(35000))
        InventoryLevel.objects.create(warehouse=gudang, product=self.kopi, on_hand=100)
        self.today = timezone.localdate()
        self.n = 0

    def order(self, channel, status="completed", quantity=2, discount=0, shipping=0, **extra):
        self.n += 1
        subtotal = Decimal(50000) * quantity
        order = Order.objects.create(store=self.store, channel=channel, status=status,
            customer_name="B", phone="-", shipping_address="-", subtotal=subtotal,
            discount=Decimal(discount), shipping_cost=Decimal(shipping),
            total=subtotal - Decimal(discount) + Decimal(shipping),
            idempotency_key=f"r-{self.n}", **extra)
        OrderItem.objects.create(order=order, product=self.kopi, name="Kopi", sku="KOPI",
            unit_price=Decimal(50000), quantity=quantity, line_total=subtotal,
            unit_cost=Decimal(20000))
        return order

    def report(self, **kwargs):
        return sales_report(self.store, self.today, self.today, **kwargs)

    def by_channel(self, report):
        return {row["channel"]: row for row in report["channels"]}

    def test_channels_are_compared_side_by_side(self):
        self.order("shopee", discount=10000, shipping=5000)
        self.order("shopee")
        self.order("tiktok_shop", quantity=1)

        rows = self.by_channel(self.report())

        shopee = rows["shopee"]
        self.assertEqual((shopee["orders"], shopee["gross"], shopee["discount"],
            shopee["net_sales"], shopee["shipping"]), (2, Decimal(200000), Decimal(10000),
            Decimal(190000), Decimal(5000)))
        self.assertEqual((shopee["cogs"], shopee["margin"]), (Decimal(80000), Decimal(110000)))
        self.assertEqual(rows["tiktok_shop"]["net_sales"], Decimal(50000))

    def test_cancelled_and_returned_orders_are_not_sales(self):
        self.order("shopee")
        self.order("shopee", status="cancelled")
        self.order("shopee", status="returned")
        self.order("shopee", status="pending")  # unpaid: not in the report at all

        row = self.by_channel(self.report())["shopee"]

        self.assertEqual((row["orders"], row["net_sales"]), (1, Decimal(100000)))
        self.assertEqual((row["cancelled_count"], row["return_count"]), (1, 1))
        self.assertEqual(row["returns"], Decimal(100000))
        # One sale, one cancellation, one return: two of three did not stick.
        self.assertEqual(row["cancel_pct"], Decimal("66.7"))

    def test_mitra_sales_are_valued_at_the_mitra_price(self):
        partner = ConsignmentPartner.objects.create(store=self.store, name="AB Shop")
        self.order("abshop", order_type="reseller_sell_through", consignment_partner=partner,
            quantity=2)

        row = self.by_channel(self.report())["abshop"]

        self.assertEqual(row["net_sales"], Decimal(70000))  # 2 × 35.000, not 2 × 50.000
        self.assertEqual(row["margin"], Decimal(30000))

    def test_marketplace_fees_reduce_the_margin_and_coverage_is_shown(self):
        from apps.channels.models import ChannelOrder, ChannelOrderIncome, SalesChannel
        channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="S")
        first, second = self.order("shopee"), self.order("shopee")
        link = ChannelOrder.objects.create(channel=channel, order=first, external_order_id="S1")
        ChannelOrder.objects.create(channel=channel, order=second, external_order_id="S2")
        ChannelOrderIncome.objects.create(channel_order=link, commission=Decimal(6000),
            voucher=Decimal(4000), net_released=Decimal(90000))

        row = self.by_channel(self.report())["shopee"]

        self.assertEqual((row["fees"], row["fee_orders"], row["fees_known"]), (Decimal(10000), 1,
            False))
        self.assertEqual(row["margin"], Decimal(200000 - 80000 - 10000))

    def test_channel_filter_products_and_customers(self):
        customer = Customer.objects.create(store=self.store, name="Sari")
        self.order("shopee", customer=customer)
        self.order("web")

        report = self.report(channel="shopee")

        self.assertEqual([row["channel"] for row in report["channels"]], ["shopee"])
        self.assertEqual((report["products"][0]["quantity"], report["products"][0]["revenue"]),
            (2, Decimal(100000)))
        self.assertEqual(report["customers"][0]["name"], "Sari")

    def test_page_and_csv(self):
        self.order("shopee")
        self.client.force_login(self.owner)
        url = f"/merchant/sales/laporan/?store={self.store.id}"

        self.assertContains(self.client.get(url), "Shopee")
        csv = self.client.get(url + "&format=csv")
        self.assertEqual(csv["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Shopee,1,100000", csv.content.decode())
