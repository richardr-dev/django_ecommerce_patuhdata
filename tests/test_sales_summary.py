"""Ringkasan penjualan: one service for the page and its partial, refunds taken off, bars that keep their width."""
from decimal import Decimal

from django.urls import reverse
from django.utils import translation
from test_sales_docs import Base

from apps.channels.sales_summary import summary
from apps.orders.models import Order, PaymentTransaction, Refund


class SalesSummaryTests(Base):
    def setUp(self):
        super().setUp()
        self.a = self.invoice(kabel=2)
        self.b = self.invoice(kabel=1)
        Order.objects.filter(pk__in=[self.a.order_id, self.b.order_id]).update(status="paid")

    def test_refunds_come_off_the_sales(self):
        before = summary(self.store, {})
        order = Order.objects.get(pk=self.b.order_id)
        payment = PaymentTransaction.objects.create(order=order, provider="demo", external_id="sum-1",
            amount=order.total, status="succeeded", kind="payment")
        Refund.objects.create(order=order, payment=payment, amount=Decimal(50_000), reason="OTHERS",
            status="succeeded", actor=self.owner, method="manual_transfer")
        after = summary(self.store, {})
        self.assertEqual(after["gross"], before["gross"])
        self.assertEqual(after["revenue"], before["revenue"] - 50_000)
        self.assertEqual(after["refunds"], 50_000)

    def test_bar_widths_use_a_dot_in_indonesian(self):
        with translation.override("id"):
            data = summary(self.store, {})
            for row in data["channel_rows"] + data["daily"] + data["monthly"]:
                self.assertNotIn(",", row["pct"])

    def test_page_and_partial_render(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-sales"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Ringkasan penjualan")
        self.assertContains(page, "bb-bar-row")
        part = self.client.get(reverse("merchant-sales-stats"))
        self.assertNotContains(part, "<html")
        self.assertContains(part, "Penjualan bersih")

    def test_swapped_dates_are_put_in_order(self):
        data = summary(self.store, {"date_from": "2026-12-31", "date_to": "2026-01-01"})
        self.assertLess(data["date_from"], data["date_to"])


class PaymentLinkPageTests(Base):
    def test_pending_order_shows_link_and_whatsapp(self):
        invoice = self.invoice(kabel=1)
        Order.objects.filter(pk=invoice.order_id).update(status="pending")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-order-payment-link", args=[invoice.order_id]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Kirim via WhatsApp")
        self.assertContains(page, 'id="pay-link"')
