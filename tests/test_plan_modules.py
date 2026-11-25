"""Starter is the marketplace package: orders, invoices, products, stock, customers and settings. The
accounting modules (purchasing, cash and bank, ledger, reports, tax, mitra, POS) are hidden and answer with
an upgrade notice; Pro and a store without a subscription keep everything."""
from django.test import Client, TestCase
from django.urls import reverse
from test_platform import _store

from apps.billing.models import Plan
from apps.billing.modules import path_open


class StarterPackageTests(TestCase):
    def client_for(self, plan_code):
        store = _store(f"t-{plan_code}", plan=Plan.objects.get(code=plan_code))
        client = Client()
        client.force_login(store.owner)
        return client

    def test_starter_is_the_marketplace_package_and_pro_has_full_books(self):
        starter, pro = Plan.objects.get(code="free"), Plan.objects.get(code="pro")
        self.assertFalse(starter.full_books)
        self.assertTrue(pro.full_books)
        self.assertIn("marketplace", starter.tagline)
        self.assertNotIn("399.000", pro.features)
        self.assertIn("Akuntansi lengkap", pro.features)

    def test_which_pages_starter_opens(self):
        for name in ("merchant-dashboard", "merchant-sales-invoices", "merchant-import", "merchant-sku-map",
                "merchant-customers", "merchant-stock", "merchant-billing", "merchant-channel-recap",
                "merchant-stock-adjustments"):
            self.assertTrue(path_open(reverse(name)), name)
        for name in ("merchant-bills", "merchant-journal-list", "merchant-profit-loss", "merchant-tax",
                "merchant-bank-accounts", "merchant-receivables", "merchant-pos", "merchant-quotes",
                "settlement-partners", "merchant-accounting", "merchant-payroll", "merchant-opening-balances"):
            self.assertFalse(path_open(reverse(name)), name)
        self.assertFalse(path_open("/books/accounting/bills/123/"))      # a detail page follows its list
        self.assertFalse(path_open("/kasir/"))

    def test_starter_gets_the_notice_and_a_sidebar_without_the_books(self):
        client = self.client_for("free")
        page = client.get(reverse("merchant-profit-loss"))
        self.assertEqual(page.status_code, 402)
        self.assertContains(page, "belum termasuk paket", status_code=402)
        home = client.get(reverse("merchant-dashboard"))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Impor pesanan")
        self.assertNotContains(home, "Laba rugi")
        self.assertNotContains(home, reverse("merchant-bill-new"))     # nor in Buat baru
        self.assertEqual(client.get(reverse("merchant-import")).status_code, 200)

    def test_pro_keeps_everything(self):
        client = self.client_for("pro")
        self.assertEqual(client.get(reverse("merchant-profit-loss")).status_code, 200)
        self.assertContains(client.get(reverse("merchant-dashboard")), "Laba rugi")
