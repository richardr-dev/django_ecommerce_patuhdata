"""Selisih & klaim marketplace: recorded against Piutang marketplace, settled through the bank, always tied."""
import datetime
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import marketplace_adjustments as adj
from apps.accounting.controls import run_checks
from apps.accounting.ledger import (
    MARKETPLACE,
    OTHER_RECEIVABLE,
    account_balances,
    chart_of_accounts,
)
from apps.accounting.models import Account, MarketplaceAdjustment


class MarketplaceAdjustmentTests(Base):
    def setUp(self):
        super().setUp()
        chart_of_accounts(self.store)
        self.shipping = Account.objects.get(store=self.store, code="6130")

    def balance(self, code):
        return {a.code: a.balance for a in account_balances(self.store)}.get(code, Decimal(0))

    def tie(self):
        return next(c for c in run_checks(self.store, self.today) if c.key == "providers")

    def charge(self, amount=15_000, account=None):
        return adj.record(self.store, provider="shopee", kind=MarketplaceAdjustment.CHARGE,
            account=account or self.shipping, amount=amount, date=self.today, description="Selisih ongkir")

    def test_charge_books_expense_against_the_marketplace_and_ties(self):
        self.charge()
        self.assertEqual(self.balance("6130"), 15_000)
        self.assertEqual(self.balance(MARKETPLACE), -15_000)
        self.assertEqual(self.tie().status, "ok")

    def test_settling_a_charge_takes_it_out_of_the_bank(self):
        item = self.charge()
        adj.settle(item, date=self.today)
        self.assertEqual(self.balance(MARKETPLACE), 0)
        self.assertEqual(self.balance("1100"), -15_000)
        self.assertEqual(self.tie().status, "ok")
        adj.unsettle(item)
        self.assertEqual(self.balance(MARKETPLACE), -15_000)

    def test_claim_to_piutang_lainnya_and_void(self):
        other = Account.objects.get(store=self.store, code=OTHER_RECEIVABLE)
        self.assertEqual(other.name, "Piutang lainnya")
        claim = adj.record(self.store, provider="shopee", kind=MarketplaceAdjustment.CLAIM,
            account=Account.objects.get(store=self.store, code="7100"), amount=20_000, date=self.today,
            description="Kompensasi paket hilang")
        self.assertEqual(self.balance(MARKETPLACE), 20_000)
        adj.void(claim)
        self.assertEqual(self.balance(MARKETPLACE), 0)

    def test_refusals(self):
        with self.assertRaises(adj.AdjustmentError):
            self.charge(amount=0)
        bank = Account.objects.get(store=self.store, code="1100")
        with self.assertRaises(adj.AdjustmentError):
            self.charge(account=bank)   # a money account is settled, not adjusted against
        item = self.charge()
        adj.settle(item, date=self.today)
        with self.assertRaises(adj.AdjustmentError):
            adj.void(item)
        with self.assertRaises(adj.AdjustmentError):
            adj.settle(item, date=self.today - datetime.timedelta(days=1))

    def test_page_records_once(self):
        self.client.force_login(self.owner)
        url = reverse("merchant-marketplace-adjustments")
        self.assertEqual(self.client.get(url).status_code, 200)
        data = {"action": "create", "idem": "k1", "provider": "shopee", "kind": "charge",
            "account": self.shipping.pk, "amount": "15.000", "date": self.today.isoformat(),
            "description": "Selisih ongkir"}
        self.client.post(url, data)
        self.client.post(url, data)
        self.assertEqual(MarketplaceAdjustment.objects.filter(store=self.store).count(), 1)
        self.assertEqual(MarketplaceAdjustment.objects.get().amount, 15_000)
        self.assertContains(self.client.get(url), "Selisih ongkir")
