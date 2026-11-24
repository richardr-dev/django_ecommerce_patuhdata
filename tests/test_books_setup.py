"""Starting the books: the Beranda checklist, and products imported from a CSV leave stock movements."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting import bank, opening
from apps.accounting.models import BankAccount
from apps.catalog.models import Product
from apps.orders.models import InventoryMovement
from apps.orders.onboarding import books_setup_steps
from apps.orders.setup_views import import_products, parse_import_rows
from apps.orders.stock import stock_audit
from apps.shops.models import Store
from apps.tax.models import TaxProfile
from apps.tax.sales import create_sales_invoice


def row(sku="KOP-1", stock="12", cost="9000"):
    return {"name": "Kopi sachet", "sku": sku, "price": "12000", "stock": stock, "cost_price": cost}


class ImportLeavesMovementsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("imp-owner")
        self.store = Store.objects.create(owner=self.owner, name="Warung", slug="warung")

    def test_import_and_reimport_are_explained_by_movements(self):
        import_products(self.store, parse_import_rows([row(stock="12")]), actor=self.owner)
        import_products(self.store, parse_import_rows([row(stock="7")]), actor=self.owner)
        import_products(self.store, parse_import_rows([row(stock="7")]), actor=self.owner)  # no change

        self.assertEqual(stock_audit(self.store), [])
        moves = list(InventoryMovement.objects.filter(store=self.store).order_by("id")
            .values_list("previous_on_hand", "new_on_hand", "kind", "actor"))
        self.assertEqual(moves, [(0, 12, "adjust", self.owner.id), (12, 7, "adjust", self.owner.id)])


class BooksSetupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("setup-owner")
        self.store = Store.objects.create(owner=self.owner, name="Warung", slug="warung")

    def steps(self):
        return {step.title: step for step in books_setup_steps(self.store)}

    def test_a_new_shop_starts_with_nothing_done_even_with_the_automatic_cash_account(self):
        bank.bank_accounts(self.store)  # creates "Kas & bank utama"
        self.assertEqual([step.done for step in books_setup_steps(self.store)], [False] * 5)

    def test_each_step_ticks_from_real_data(self):
        TaxProfile.objects.create(store=self.store, npwp="3171234567890001")
        main = bank.bank_accounts(self.store)[0]
        BankAccount.objects.filter(pk=main.pk).update(name="Laci kasir")
        import_products(self.store, parse_import_rows([row()]))
        opening.post_opening(self.store, date=timezone.localdate(), banks={main: Decimal(500_000)})
        create_sales_invoice(self.store, lines=[{"product": Product.objects.get(), "qty": 1,
            "unit_price": Decimal(12_000)}], buyer_name="Umum", issued_on=timezone.localdate())

        self.assertEqual([step.done for step in books_setup_steps(self.store)], [True] * 5)

    def test_products_without_cost_are_named_in_the_hint(self):
        import_products(self.store, parse_import_rows([row(cost=""), row(sku="KOP-2")]))
        step = self.steps()["Masukkan produk dan harga pokok"]
        self.assertFalse(step.done)
        self.assertIn("1 produk belum punya harga pokok", step.hint)

    def test_tax_hint_says_what_is_set_now(self):
        TaxProfile.objects.create(store=self.store)
        self.assertIn("orang pribadi, PPh Final 0,5% dari omzet, bukan PKP",
            self.steps()["Periksa profil pajak"].hint)

    def test_beranda_shows_progress_until_everything_is_done(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("merchant-dashboard"))
        self.assertContains(response, "Siapkan pembukuan")
        self.assertContains(response, "0 dari 5 selesai")
