"""Sales order → partial deliveries (surat jalan, stock out, 1410) → invoice from deliveries (HPP from
1410) → backorder and closing. The worked example: order 10 units at Rp100.000, cost Rp60.000."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting.ledger import account_balances
from apps.accounting.models import JournalEntry
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import InventoryMovement
from apps.shops.models import Customer, Store
from apps.tax import quotes, sales_orders
from apps.tax.models import Delivery, Invoice, SalesOrder
from apps.tax.posting import void_invoice


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        self.kabel = Product.objects.create(store=self.store, name="Kabel", slug="kabel", sku="KBL", price=100_000,
            cost_price=60_000)
        self.level = InventoryLevel.objects.create(warehouse=self.warehouse, product=self.kabel, on_hand=8)
        self.ani = Customer.objects.create(store=self.store, name="Toko Ani", phone="0811", payment_terms_days=30)
        self.client.force_login(self.owner)
        self.today = timezone.localdate()

    def order(self, qty=10):
        return sales_orders.create_sales_order(self.store, lines=[{"product": self.kabel, "qty": qty,
            "unit_price": Decimal(100_000)}], buyer_name="Toko Ani", customer=self.ani, actor=self.owner)

    def shelf(self):
        self.level.refresh_from_db()
        return self.level.on_hand


class WorkedExampleTests(Base):
    def test_order_deliver_six_invoice_deliver_rest_close(self):
        order = self.order(10)
        self.assertEqual((order.status, order.number[:3], order.total), ("open", "SO/", Decimal(1_000_000)))
        self.assertEqual(self.shelf(), 8)                                   # an order holds no stock
        self.assertFalse(JournalEntry.objects.filter(store=self.store).exists())

        line = order.lines.get()
        first = sales_orders.create_delivery(order, quantities={line.pk: 6}, courier="jne", tracking_number="JN1",
            actor=self.owner)
        self.assertEqual((first.number[:3], first.value), ("SJ/", Decimal(360_000)))
        self.assertEqual(self.shelf(), 2)                                   # 6 left the shelf
        self.assertEqual(InventoryMovement.objects.filter(kind="issue", source_id=str(first.pk)).count(), 1)
        self.assertEqual(balance(self.store, "1410"), Decimal(360_000))     # cost waits in 1410
        self.assertEqual(balance(self.store, "1400"), Decimal(-360_000))    # (no opening: Persediaan fell by the cost)
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((order.status, line.remaining), ("partial", 4))    # 4 on backorder

        invoice = sales_orders.invoice_deliveries(order, [first], on_credit=True, due_date=self.today, actor=self.owner)
        self.assertEqual((invoice.subtotal, invoice.from_deliveries, invoice.sales_order_id), (Decimal(600_000), True, order.pk))
        self.assertEqual(self.shelf(), 2)                                   # the invoice takes no stock again
        self.assertEqual(balance(self.store, "1410"), Decimal(0))           # HPP booked from 1410...
        self.assertEqual(balance(self.store, "5100"), Decimal(360_000))     # ...not from Persediaan
        self.assertEqual(balance(self.store, "1400"), Decimal(-360_000))
        self.assertEqual(balance(self.store, "4100"), Decimal(600_000))
        first.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((first.invoice_id, line.invoiced_quantity), (invoice.pk, 6))

        with self.assertRaises(sales_orders.SalesOrderError):              # only 2 on the shelf for 4
            sales_orders.create_delivery(order, quantities={line.pk: 4}, actor=self.owner)
        self.level.on_hand = 10
        self.level.save()
        second = sales_orders.create_delivery(order, quantities={line.pk: 4}, actor=self.owner)
        order.refresh_from_db()
        self.assertEqual(order.status, "fulfilled")
        with self.assertRaises(sales_orders.SalesOrderError):              # nothing left to deliver
            sales_orders.create_delivery(order, quantities={line.pk: 1}, actor=self.owner)
        second_invoice = sales_orders.invoice_deliveries(order, [second], on_credit=False, actor=self.owner)
        self.assertEqual((second_invoice.subtotal, second_invoice.status), (Decimal(400_000), "paid"))
        self.assertEqual(balance(self.store, "5100"), Decimal(600_000))
        self.assertEqual(sales_orders.delivered_not_invoiced(self.store), Decimal(0))

    def test_closing_drops_the_backorder_and_cancelling_a_delivery_restores_the_shelf(self):
        order = self.order(10)
        line = order.lines.get()
        delivery = sales_orders.create_delivery(order, quantities={line.pk: 3}, actor=self.owner)
        self.assertEqual(self.shelf(), 5)
        sales_orders.close_order(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "closed")
        self.assertEqual(sales_orders.backorder(self.store).count(), 0)
        sales_orders.cancel_delivery(delivery, actor=self.owner)
        self.assertEqual(self.shelf(), 8)
        self.assertEqual(balance(self.store, "1410"), Decimal(0))
        line.refresh_from_db()
        self.assertEqual(line.delivered_quantity, 0)
        untouched = self.order(2)
        sales_orders.close_order(untouched)
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "cancelled")                     # nothing delivered: cancelled, not closed

    def test_voiding_the_invoice_reopens_the_deliveries_without_touching_stock(self):
        order = self.order(5)
        line = order.lines.get()
        delivery = sales_orders.create_delivery(order, quantities={line.pk: 5}, actor=self.owner)
        invoice = sales_orders.invoice_deliveries(order, [delivery], actor=self.owner)
        with self.assertRaises(sales_orders.SalesOrderError):
            sales_orders.cancel_delivery(delivery)                           # invoiced: void the invoice first
        void_invoice(invoice, actor=self.owner)
        self.assertEqual(self.shelf(), 3)                                   # goods stay with the buyer
        self.assertEqual(balance(self.store, "1410"), Decimal(300_000))     # back to "delivered, not invoiced"
        self.assertEqual(balance(self.store, "5100"), Decimal(0))
        delivery.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((delivery.invoice_id, line.invoiced_quantity), (None, 0))
        again = sales_orders.invoice_deliveries(order, [delivery], actor=self.owner)
        self.assertEqual(again.subtotal, Decimal(500_000))

    def test_a_product_without_cost_cannot_be_delivered_and_the_control_ties(self):
        from apps.accounting.controls import run_checks
        tali = Product.objects.create(store=self.store, name="Tali", slug="tali", sku="TL", price=10_000)
        InventoryLevel.objects.create(warehouse=self.warehouse, product=tali, on_hand=5)
        order = sales_orders.create_sales_order(self.store, lines=[{"product": tali, "qty": 2, "unit_price": Decimal(10_000)}],
            buyer_name="Budi", actor=self.owner)
        with self.assertRaises(sales_orders.SalesOrderError):
            sales_orders.create_delivery(order, quantities={order.lines.get().pk: 2}, actor=self.owner)
        kabel_order = self.order(3)
        sales_orders.create_delivery(kabel_order, quantities={kabel_order.lines.get().pk: 3}, actor=self.owner)
        check = next(c for c in run_checks(self.store, self.today) if c.key == "delivered")
        self.assertEqual((check.status, check.book, check.other), ("ok", Decimal(180_000), Decimal(180_000)))


class SalesOrderPagesTests(Base):
    def test_create_from_the_form_deliver_and_invoice_from_the_page(self):
        reply = self.client.post(reverse("merchant-sales-order-new"), {"customer_id": str(self.ani.id), "buyer_name": "Toko Ani",
            "issued_on": self.today.isoformat(), "product[]": [self.kabel.pk], "qty[]": ["4"], "unit_price[]": ["100000"],
            "discount": "0", "shipping": "0"})
        order = SalesOrder.objects.get(store=self.store)
        self.assertRedirects(reply, reverse("merchant-sales-order-detail", args=[order.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("merchant-sales-order-detail", args=[order.pk]))
        self.assertContains(page, "Buat surat jalan")
        line = order.lines.get()
        reply = self.client.post(reverse("merchant-sales-order-detail", args=[order.pk]), {"action": "deliver",
            f"qty_{line.pk}": "3", "delivered_on": self.today.isoformat(), "courier": "jne", "tracking_number": "JN9"})
        delivery = Delivery.objects.get(sales_order=order)
        self.assertRedirects(reply, reverse("merchant-delivery-detail", args=[delivery.pk]), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("merchant-delivery-print", args=[delivery.pk])), "SURAT JALAN")
        self.assertContains(self.client.get(reverse("merchant-deliveries")), delivery.number)
        reply = self.client.post(reverse("merchant-sales-order-detail", args=[order.pk]), {"action": "invoice",
            "delivery": [str(delivery.pk)], "due_days": "30"})
        invoice = Invoice.objects.get(sales_order=order)
        self.assertRedirects(reply, reverse("merchant-invoice-detail", args=[invoice.pk]), fetch_redirect_response=False)
        self.assertEqual(invoice.subtotal, Decimal(300_000))
        detail = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]))
        self.assertContains(detail, "surat jalan")                           # the invoice says where it came from
        self.assertContains(self.client.get(reverse("merchant-sales-orders")), order.number)
        self.assertContains(self.client.get(reverse("merchant-sales-orders")), "1")  # backorder tile

    def test_a_quote_becomes_a_sales_order(self):
        quote = quotes.save_quote(self.store, lines=[{"product": self.kabel, "qty": 2, "unit_price": Decimal(95_000)}],
            buyer_name="Toko Ani", customer=self.ani, actor=self.owner)
        self.assertContains(self.client.get(reverse("merchant-quote-detail", args=[quote.pk])), "pesanan penjualan")
        reply = self.client.post(reverse("merchant-sales-order-from-quote", args=[quote.pk]))
        order = SalesOrder.objects.get(quote=quote)
        self.assertRedirects(reply, reverse("merchant-sales-order-detail", args=[order.pk]), fetch_redirect_response=False)
        self.assertEqual((order.lines.get().unit_price, order.buyer_name), (Decimal(95_000), "Toko Ani"))
        quote.refresh_from_db()
        self.assertEqual(quote.status, "accepted")
        self.assertEqual(self.client.post(reverse("merchant-sales-order-from-quote", args=[quote.pk]), follow=True).status_code, 200)
        self.assertEqual(SalesOrder.objects.filter(quote=quote).count(), 1)
