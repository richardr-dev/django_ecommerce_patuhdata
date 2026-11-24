"""The book currency: one setting, Rupiah today, USD/EUR ready; fixed once the books have an entry."""
from decimal import Decimal

from django.template import Context, Template
from django.urls import reverse
from test_sales_docs import Base

from apps.shops import currency


class FormattingTests(Base):
    def tearDown(self):
        currency.reset()
        super().tearDown()

    def test_rupiah_matches_the_old_output(self):
        idr = currency.get("IDR")
        self.assertEqual(idr.format(1250000), "Rp1.250.000")
        self.assertEqual(idr.format(Decimal("-15000.4")), "−Rp15.000")
        self.assertEqual(idr.format(150000, signed=True), "+Rp150.000")
        self.assertEqual(idr.format(Decimal("0.3")), "Rp0")

    def test_dollar_and_euro_are_ready(self):
        self.assertEqual(currency.get("USD").format(Decimal("1250000.5")), "$1,250,000.50")
        self.assertEqual(currency.get("EUR").format(Decimal("-9.99")), "−€9,99")

    def test_filters_follow_the_active_currency(self):
        template = Template("{% load books %}{{ v|rp }} {{ v|signed_rp }}")
        self.assertEqual(template.render(Context({"v": 1500})), "Rp1.500 +Rp1.500")
        currency.activate("USD")
        self.assertEqual(template.render(Context({"v": 1500})), "$1,500.00 +$1,500.00")


class SettingTests(Base):
    def test_rupiah_is_the_default_and_only_choice_today(self):
        self.assertEqual(self.store.currency, "IDR")
        with self.assertRaises(currency.CurrencyError):
            currency.set_currency(self.store, "USD")

    def test_locked_once_the_books_have_an_entry(self):
        self.assertTrue(currency.can_change(self.store)[0])
        self.invoice(kabel=1)
        allowed, reason = currency.can_change(self.store)
        self.assertFalse(allowed)
        self.assertIn("jurnal", reason)

    def test_page(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-currency-settings"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Rupiah (IDR)")
        self.assertContains(page, "Segera")
        self.assertContains(page, 'data-currency="IDR"')
        response = self.client.post(reverse("merchant-currency-settings"), {"currency": "EUR"})
        self.assertEqual(response.status_code, 302)
        self.store.refresh_from_db()
        self.assertEqual(self.store.currency, "IDR")
