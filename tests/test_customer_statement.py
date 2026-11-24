"""Laporan rekening pelanggan: opening balance, activity with a running balance, closing balance."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.tax import credit_notes
from apps.tax.credit_limit import owed
from apps.tax.customer_statement import build
from apps.tax.posting import void_invoice
from apps.tax.services import record_payment
from apps.tax.writeoff import write_off_invoice


class StatementTests(Base):
    def test_activity_with_running_and_closing_balance(self):
        start = self.today - timedelta(days=10)
        before = self.invoice(kabel=1, days_ago=20)          # 111.000, before the period
        record_payment(before, Decimal(11_000), self.today - timedelta(days=15), "transfer")
        inside = self.invoice(kabel=2, days_ago=5)           # 222.000
        record_payment(inside, Decimal(100_000), self.today - timedelta(days=2), "transfer")
        credit_notes.create_credit_note(inside, lines=self.lines(inside), issued_on=self.today)  # 111.000
        voided = self.invoice(kabel=1, days_ago=3)
        void_invoice(voided)

        s = build(self.store, self.customer, start, self.today)

        self.assertEqual(s.opening, Decimal(100_000))
        self.assertEqual([(l["kind"], l["debit"], l["credit"], l["balance"]) for l in s.lines], [
            ("invoice", Decimal(222_000), 0, Decimal(322_000)),
            ("payment", 0, Decimal(100_000), Decimal(222_000)),
            ("credit_note", 0, Decimal(111_000), Decimal(111_000))])
        self.assertEqual((s.invoiced, s.received, s.closing), (Decimal(222_000), Decimal(211_000), Decimal(111_000)))
        self.assertEqual(s.closing, owed(self.customer))  # ties to the open invoices

    def test_write_off_and_cash_sale_net_to_nothing_owed(self):
        bad = self.invoice(kabel=1, days_ago=1)
        write_off_invoice(bad, reason="Pelanggan tutup usaha", date=self.today)
        self.invoice(kabel=1, credit=False)  # paid on the spot

        s = build(self.store, self.customer, self.today - timedelta(days=30), self.today)
        self.assertEqual(s.closing, 0)
        self.assertIn("writeoff", [l["kind"] for l in s.lines])
        self.assertEqual(s.closing, owed(self.customer))

    def test_page_prints_for_the_period(self):
        self.invoice(kabel=1, days_ago=2)
        self.client.force_login(self.owner)
        url = reverse("merchant-customer-statement", args=[self.customer.id])
        response = self.client.get(url, {"dari": (self.today - timedelta(days=7)).isoformat(),
            "sampai": self.today.isoformat()})
        self.assertContains(response, "LAPORAN REKENING PELANGGAN")
        self.assertEqual(response.context["s"].closing, Decimal(111_000))
        self.assertContains(response, "Saldo akhir per")
        self.assertContains(self.client.get(reverse("merchant-customer-detail", args=[self.customer.id])),
            url)
