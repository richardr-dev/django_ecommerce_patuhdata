"""PPh 22 collected by a marketplace (PMK 37/2025) and the monthly marketplace tax pack for the accountant.

Worked example: two Shopee orders of 100.000 each. The income report releases S1 with commission 6.000 and PPh 22
500 (0,5% of 100.000): 93.500 reaches the bank. PPh 22 is tax, not a fee: under PPh Final UMKM it is part of the
month's final tax (6990) and only the rest of the 0,5% is deposited; under the general rates it is prepaid tax
(1525), a credit in the SPT Tahunan."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from test_marketplace_income import _xlsx

from apps.accounting.controls import _balances, marketplace_pph22_tie
from apps.accounting.ledger import (
    MARKETPLACE_PPH22,
    PPH_FINAL_EXPENSE,
    account_balances,
    post_pending_entries,
)
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels import marketplace_tax
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.income import import_income
from apps.channels.models import ChannelOrderIncome, SalesChannel
from apps.channels.parsers.income import parse_income
from apps.channels.services import import_order
from apps.shops.models import Store
from apps.tax import fiscal
from apps.tax.models import TaxProfile
from apps.tax.services import issue_pending_invoices, month_bounds, monthly_tax

D = Decimal
HEADER = ["No. Pesanan", "Tanggal Dana Dilepaskan", "Total Penghasilan", "Biaya Komisi", "PPh 22 (0,5%)"]


def _balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, D(0))


class ParserTests(TestCase):
    def test_pph22_is_read_apart_from_the_fees(self):
        rows = parse_income(_xlsx([["S1", "2026-11-10", 93500, -6000, -500]], header=HEADER))
        self.assertEqual(rows[0].fees, {"commission": D(6000), "pph22": D(500)})

    def test_a_pajak_penghasilan_column_is_never_the_amount_released(self):
        header = ["No. Pesanan", "Pajak Penghasilan", "Jumlah Dana Dilepas"]
        rows = parse_income(_xlsx([["S1", 500, 93500]], header=header))
        self.assertEqual((rows[0].net, rows[0].fees), (D(93500), {"pph22": D(500)}))


class Base(TestCase):
    regime = "umkm_final"

    def setUp(self):
        self.owner = User.objects.create_user("mp-tax-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-mp-tax")
        TaxProfile.objects.create(store=self.store, taxpayer_type="company", pph_regime=self.regime)
        gudang = Warehouse.objects.create(store=self.store, name="G", code="G1")
        kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=50000,
            cost_price=30000)
        InventoryLevel.objects.create(warehouse=gudang, product=kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Kopi Official")
        for order_id in ("S1", "S2"):
            import_order(self.channel, MarketplaceOrder(external_id=order_id, buyer_name="B", phone="0812",
                address="x", lines=[OrderLine(sku="KOPI", quantity=2, unit_price=D(50000))]))
        post_pending_entries()
        issue_pending_invoices()
        self.today = timezone.localdate()
        rows = parse_income(_xlsx([["S1", self.today.isoformat(), 93500, -6000, -500]], header=HEADER))
        self.summary = import_income(self.channel, rows)


class UmkmTests(Base):
    def test_pph22_is_tax_not_a_fee_and_settles_part_of_the_final_tax(self):
        self.assertEqual((self.summary["fees"], self.summary["pph22"], self.summary["net"]),
            (D(6000), D(500), D(93500)))
        income = ChannelOrderIncome.objects.get()
        self.assertEqual((income.commission, income.other_fee, income.pph22, income.total_fees),
            (D(6000), D(0), D(500), D(6000)))
        self.assertEqual((_balance(self.store, PPH_FINAL_EXPENSE), _balance(self.store, MARKETPLACE_PPH22)),
            (D(500), D(0)))
        tax = monthly_tax(self.store, self.today.year, self.today.month)
        self.assertEqual((tax.turnover, tax.pph, tax.marketplace_pph22, tax.pph_to_deposit),
            (D(200000), D(1000), D(500), D(500)))
        check = marketplace_pph22_tie(self.store, self.today, _balances(self.store))
        self.assertEqual(check.status, "ok")


class GeneralRatesTests(Base):
    regime = "normal"

    def test_pph22_is_prepaid_tax_and_a_credit_in_the_annual_return(self):
        self.assertEqual((_balance(self.store, MARKETPLACE_PPH22), _balance(self.store, PPH_FINAL_EXPENSE)),
            (D(500), D(0)))
        check = marketplace_pph22_tie(self.store, self.today, _balances(self.store))
        self.assertEqual((check.status, check.book, check.other), ("ok", D(500), D(500)))
        sheet = fiscal.worksheet(self.store, self.today.year)
        self.assertEqual(sheet["marketplace_withheld"], D(500))
        self.assertEqual(sheet["credits_total"], D(500))


class PackTests(Base):
    def test_the_month_per_shop_per_order_and_what_to_fix(self):
        start, end = month_bounds(self.today.year, self.today.month)
        pack = marketplace_tax.build(self.store, start, end)
        [shop] = pack["shops"]
        self.assertEqual((shop["invoices"], shop["total"], shop["released_count"], shop["fees_total"], shop["pph22"],
            shop["net"], shop["gross_released"]), (2, D(200000), 1, D(6000), D(500), D(93500), D(100000)))
        self.assertEqual((shop["awaiting_count"], shop["awaiting_value"]), (1, D(100000)))
        self.assertEqual([row["order"] for row in pack["orders"]], ["S1", "S2"])
        self.assertEqual(pack["orders"][1]["net"], None)
        texts = " ".join(text for _level, text, _url in pack["checks"])
        self.assertIn("1 invoice marketplace belum ada laporan penghasilannya", texts)

    def test_page_and_excel(self):
        self.client.force_login(self.owner)
        url = reverse("merchant-marketplace-tax") + f"?month={self.today:%Y-%m}"
        page = self.client.get(url)
        self.assertContains(page, "Paket pajak marketplace")
        self.assertContains(page, "Kopi Official")
        self.assertContains(page, "Rp93.500")
        sheet = self.client.get(url + "&format=xlsx")
        self.assertEqual(sheet.status_code, 200)
        self.assertIn("spreadsheetml", sheet["Content-Type"])
