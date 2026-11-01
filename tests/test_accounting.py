import json
import re
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import Client, TestCase, override_settings

from apps.accounting.ledger import (
    CASH,
    DISCOUNTS,
    GATEWAY,
    REFUNDS,
    SALES,
    SHIPPING,
    UNIDENTIFIED,
    VAT,
    post_pending_entries,
)
from apps.accounting.models import JournalEntry, JournalLine
from apps.orders.models import Order, PaymentTransaction
from apps.orders.refunds import apply_refund_result, create_refund_request, record_manual_refund
from apps.orders.services import transition_order
from apps.shops.models import Store


def _lines(entry):
    """{account code: signed amount}, debit positive."""
    return {line.account.code: line.debit - line.credit for line in entry.lines.select_related("account")}


class LedgerPostingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ledger-owner")
        self.store = Store.objects.create(owner=self.owner, name="Buku", slug="buku")
        # 100.000 goods, 10.000 coupon, 11% PPN on 90.000, 15.000 shipping.
        self.order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=Decimal(100000), discount=Decimal(10000),
            tax=Decimal(9900), shipping_cost=Decimal(15000), total=Decimal(114900),
            idempotency_key="ledger-order")

    def _pay(self, **overrides):
        values = {"order": self.order, "provider": "demo", "external_id": "pay-1",
            "status": "succeeded", "amount": self.order.total, "reconciled": True}
        return PaymentTransaction.objects.create(**{**values, **overrides})

    def test_reconciled_payment_posts_balanced_sale(self):
        self._pay()

        self.assertEqual(post_pending_entries(), 1)
        entry = JournalEntry.objects.get()
        self.assertEqual(_lines(entry), {GATEWAY: Decimal(114900), DISCOUNTS: Decimal(10000),
            SALES: Decimal(-100000), SHIPPING: Decimal(-15000), VAT: Decimal(-9900)})

    def test_posting_is_idempotent(self):
        self._pay()

        post_pending_entries()
        self.assertEqual(post_pending_entries(), 0)
        call_command("post_ledger", stdout=StringIO())
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_pending_and_failed_payments_are_not_posted(self):
        self._pay(external_id="pending", status="pending")
        self._pay(external_id="failed", status="failed", reconciled=False)

        self.assertEqual(post_pending_entries(), 0)

    def test_unreconciled_payment_is_held_as_liability_not_revenue(self):
        self._pay(reconciled=False, mismatch_reason="Order status cancelled cannot accept payment")

        post_pending_entries()
        self.assertEqual(_lines(JournalEntry.objects.get()),
            {GATEWAY: Decimal(114900), UNIDENTIFIED: Decimal(-114900)})

    def test_gateway_refund_reverses_revenue_and_proportional_vat(self):
        transition_order(self.order, "paid")
        self._pay()
        refund = create_refund_request(order=self.order, amount=Decimal(57450), reason="OTHERS",
            actor=self.owner)
        apply_refund_result(refund, status="succeeded", reference="refund-1", payload={})

        self.assertEqual(post_pending_entries(), 2)
        self.assertEqual(_lines(refund.journal_entry), {REFUNDS: Decimal(52500),
            VAT: Decimal(4950), GATEWAY: Decimal(-57450)})

    def test_manual_refund_is_paid_from_cash(self):
        transition_order(self.order, "paid")
        self._pay()
        refund = create_refund_request(order=self.order, amount=self.order.total, reason="OTHERS",
            actor=self.owner, method="manual_transfer")
        record_manual_refund(refund, transfer_reference="BCA-123", actor=self.owner)

        post_pending_entries()
        self.assertEqual(_lines(refund.journal_entry)[CASH], Decimal(-114900))

    def test_every_entry_balances(self):
        transition_order(self.order, "paid")
        self._pay()
        refund = create_refund_request(order=self.order, amount=Decimal(33333), reason="OTHERS",
            actor=self.owner)
        apply_refund_result(refund, status="succeeded", reference="refund-odd", payload={})
        post_pending_entries()

        totals = JournalLine.objects.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(totals["debit"], totals["credit"])


class FinancePageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("finance-owner")
        self.store = Store.objects.create(owner=self.owner, name="Kas", slug="kas")
        order = Order.objects.create(store=self.store, customer_name="Budi", phone="0812",
            shipping_address="Bandung", subtotal=Decimal(50000), total=Decimal(50000),
            idempotency_key="finance-order")
        PaymentTransaction.objects.create(order=order, provider="demo", external_id="fin-1",
            status="succeeded", amount=order.total, reconciled=True)
        post_pending_entries()
        self.client = Client()
        self.client.force_login(self.owner)

    def test_page_shows_trial_balance(self):
        page = self.client.get("/merchant/finance/")

        self.assertContains(page, "Neraca saldo")
        self.assertContains(page, "Seimbang")
        self.assertEqual(page.context["summary"]["net_revenue"], Decimal(50000))

    def test_csv_export_lists_journal_lines(self):
        response = self.client.get("/merchant/finance/", {"format": "csv"})

        rows = response.content.decode().strip().splitlines()
        self.assertEqual(rows[0].split(",")[0], "Tanggal")
        self.assertEqual(len(rows), 3)

    def test_date_filter_excludes_other_periods(self):
        page = self.client.get("/merchant/finance/", {"start": "2000-01-01", "end": "2000-12-31"})

        self.assertEqual(list(page.context["journal_lines"]), [])

    def test_other_users_cannot_see_the_ledger(self):
        stranger = Client()
        stranger.force_login(User.objects.create_user("stranger"))
        Store.objects.create(owner=User.objects.get(username="stranger"), name="Lain", slug="lain")

        page = stranger.get("/merchant/finance/")
        self.assertEqual(page.context["summary"]["net_revenue"], 0)


@override_settings(PLATFORM_DOMAIN="lakuin.test", ALLOWED_HOSTS=["lakuin.test"],
    PLATFORM_COMPANY_NAME="PT Patuhdata Solusi Nusantara")
class PlatformCompanyTests(TestCase):
    def test_platform_home_declares_operating_company(self):
        page = Client(HTTP_HOST="lakuin.test").get("/")

        block = re.search(r'<script type="application/ld\+json">(.*?)</script>', page.content.decode())
        data = json.loads(block.group(1))
        self.assertEqual(data["name"], "PT Patuhdata Solusi Nusantara")
        self.assertEqual(data["brand"]["name"], "Lakuin")
