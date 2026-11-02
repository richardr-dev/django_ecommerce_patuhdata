"""Tax: gap-free sales invoices for every paid order and the monthly PPh Final / PPN figures."""
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.accounting.ledger import post_pending_entries
from apps.accounting.models import JournalEntry
from apps.integrations.reconciliation import confirm_payment
from apps.orders.models import Order, Refund
from apps.shops.models import Store, StoreStaff
from apps.tax.models import Invoice, TaxProfile
from apps.tax.services import (
    InvoiceError,
    issue_invoice,
    issue_pending_invoices,
    monthly_tax,
    normalize_npwp,
)


def _order(store, *, subtotal=100000, discount=0, shipping=10000, paid=True):
    tax = (subtotal - discount) * store.tax_rate / 100
    order = Order.objects.create(store=store, customer_name="Ayu", email="ayu@example.com",
        phone="0812", shipping_address="Jl. Merdeka 1, Bandung", subtotal=subtotal,
        discount=discount, tax=tax, shipping_cost=shipping,
        total=subtotal - discount + tax + shipping, payment_provider="bank_transfer",
        idempotency_key=uuid.uuid4().hex)
    if paid:
        confirm_payment(order, provider="bank_transfer", external_id=uuid.uuid4().hex,
            amount=order.total, currency="IDR", payload={}, note="Transfer diterima")
    return order


def _book_in(year, month, *orders):
    """Post the ledger and move the orders' entries into the given month."""
    post_pending_entries()
    JournalEntry.objects.filter(payment__order__in=orders).update(date=date(year, month, 10))


class TaxTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Toko Ayu", slug="toko-ayu",
            tax_rate=Decimal(11))
        self.profile = TaxProfile.objects.create(store=self.store, legal_name="CV Ayu Jaya",
            npwp="0123456789012345", address="Bandung", taxpayer_type="company", is_pkp=True)


class InvoiceTests(TaxTestCase):
    def test_invoice_copies_seller_and_amounts(self):
        invoice, created = issue_invoice(_order(self.store, discount=20000))

        self.assertTrue(created)
        self.assertRegex(invoice.number, r"^INV/\d{4}/000001$")
        self.assertEqual((invoice.seller_name, invoice.seller_npwp), ("CV Ayu Jaya",
            "0123456789012345"))
        self.assertEqual(invoice.tax_base, Decimal(80000))
        self.assertEqual((invoice.tax_rate, invoice.tax), (Decimal(11), Decimal(8800)))
        self.assertEqual(invoice.total, Decimal(98800))
        self.assertEqual(invoice.other_charges, 0)

    def test_numbers_run_on_per_store_without_duplicates(self):
        first, _ = issue_invoice(_order(self.store))
        again, created = issue_invoice(first.order)
        second, _ = issue_invoice(_order(self.store))
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain")
        elsewhere, _ = issue_invoice(_order(other))

        self.assertFalse(created)
        self.assertEqual(again, first)
        self.assertTrue(second.number.endswith("000002"))
        self.assertTrue(elsewhere.number.endswith("000001"))

    def test_unpaid_orders_get_no_invoice(self):
        unpaid = _order(self.store, paid=False)
        with self.assertRaises(InvoiceError):
            issue_invoice(unpaid)

        _order(self.store)
        self.assertEqual(issue_pending_invoices(), 1)
        self.assertEqual(issue_pending_invoices(), 0)
        self.assertFalse(Invoice.objects.filter(order=unpaid).exists())

    def test_profile_changes_do_not_rewrite_issued_invoices(self):
        invoice, _ = issue_invoice(_order(self.store))
        self.profile.legal_name = "PT Ayu Baru"
        self.profile.save()

        invoice.refresh_from_db()
        self.assertEqual(invoice.seller_name, "CV Ayu Jaya")

    def test_npwp_normalization(self):
        self.assertEqual(normalize_npwp("01.234.567.8-901.234"), "0012345678901234")
        self.assertEqual(normalize_npwp(""), "")
        with self.assertRaises(ValueError):
            normalize_npwp("12345")


class MonthlyTaxTests(TaxTestCase):
    def test_turnover_vat_and_due_dates_come_from_the_ledger(self):
        _book_in(2025, 12, _order(self.store), _order(self.store, discount=50000))

        report = monthly_tax(self.store, 2025, 12)
        # Sales excluding PPN: (100000 + 10000) + (50000 + 10000).
        self.assertEqual(report.turnover, Decimal(170000))
        self.assertEqual(report.vat, Decimal(11000 + 5500))
        self.assertEqual(report.pph, Decimal(850))
        self.assertEqual((report.pph_due, report.vat_due), (date(2026, 1, 15), date(2026, 1, 31)))
        self.assertEqual(monthly_tax(self.store, 2025, 11).turnover, 0)

    def test_refunds_reduce_turnover(self):
        order = _order(self.store)
        Refund.objects.create(order=order, amount=Decimal(60500), status="succeeded", actor=self.owner,
            method="manual_transfer", payment=order.payment_transactions.get(), reason="OTHERS")
        post_pending_entries()
        JournalEntry.objects.update(date=date(2025, 6, 10))

        report = monthly_tax(self.store, 2025, 6)
        # Half the order refunded: half the sales and half the PPN reversed.
        self.assertEqual(report.turnover, Decimal(55000))
        self.assertEqual(report.vat, Decimal(5500))

    @patch("apps.tax.services.INDIVIDUAL_EXEMPTION", Decimal(150000))
    def test_individuals_use_up_the_yearly_exemption_first(self):
        self.profile.taxpayer_type = "individual"
        self.profile.save()
        _book_in(2025, 3, _order(self.store))  # 110000 of the 150000 exemption
        _book_in(2025, 4, _order(self.store))  # 40000 exempt, 70000 taxed

        march, april = monthly_tax(self.store, 2025, 3), monthly_tax(self.store, 2025, 4)
        self.assertEqual((march.pph_base, march.pph), (0, 0))
        self.assertEqual((april.pph_base, april.pph), (Decimal(70000), Decimal(350)))
        self.assertEqual(april.turnover_ytd, Decimal(220000))

    def test_normal_regime_has_no_monthly_pph(self):
        self.profile.pph_regime = "normal"
        self.profile.save()
        _book_in(2025, 5, _order(self.store))

        self.assertEqual(monthly_tax(self.store, 2025, 5).pph, 0)


class TaxPageTests(TaxTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.owner)

    def test_profile_form_normalizes_npwp_and_requires_it_for_pkp(self):
        response = self.client.post("/merchant/tax/", {"legal_name": "CV Ayu",
            "npwp": "", "taxpayer_type": "company", "pph_regime": "umkm_final", "is_pkp": "on",
            "invoice_prefix": "inv"})
        self.assertContains(response, "PKP wajib mengisi NPWP")

        self.client.post("/merchant/tax/", {"legal_name": "CV Ayu",
            "npwp": "12.345.678.9-012.345", "taxpayer_type": "company",
            "pph_regime": "umkm_final", "invoice_prefix": "inv"})
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.npwp, self.profile.invoice_prefix, self.profile.is_pkp),
            ("0123456789012345", "INV", False))

    def test_non_pkp_charging_vat_is_warned(self):
        self.profile.is_pkp = False
        self.profile.save()

        self.assertContains(self.client.get("/merchant/tax/"), "padahal belum PKP")

    def test_viewers_can_read_but_not_change_the_profile(self):
        viewer = User.objects.create_user("viewer")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        client = Client()
        client.force_login(viewer)

        self.assertEqual(client.get("/merchant/tax/").status_code, 200)
        self.assertEqual(client.post("/merchant/tax/", {"legal_name": "X"}).status_code, 404)

    def test_register_csv_lists_the_months_invoices_for_coretax(self):
        invoice, _ = issue_invoice(_order(self.store))
        order = invoice.order
        Refund.objects.create(order=order, amount=Decimal(1000), status="succeeded", actor=self.owner,
            method="manual_transfer", payment=order.payment_transactions.get(), reason="OTHERS")
        month = invoice.issued_on.strftime("%Y-%m")

        rows = self.client.get(f"/merchant/tax/invoices.csv?month={month}").content.decode()
        header, line = rows.strip().splitlines()
        self.assertIn("DPP nilai lain (11/12)", header)
        self.assertIn(invoice.number, line)
        self.assertIn(",100000.00,91666.67,11.00,11000.00,10000.00,121000.00,1000.00", line)

    def test_seller_adds_buyer_npwp_and_buyer_opens_their_copy(self):
        invoice, _ = issue_invoice(_order(self.store))
        self.client.post(f"/merchant/invoices/{invoice.id}/", {"buyer_npwp": "3201234567890001"})
        invoice.refresh_from_db()
        self.assertEqual(invoice.buyer_npwp, "3201234567890001")

        order = invoice.order
        buyer = Client()
        self.assertEqual(buyer.get(f"/orders/{order.id}/invoice/").status_code, 404)
        page = buyer.get(f"/orders/{order.id}/invoice/?token={order.access_token}")
        self.assertContains(page, invoice.number)
        self.assertContains(page, "NPWP 0123456789012345")

    def test_other_sellers_cannot_open_the_invoice(self):
        invoice, _ = issue_invoice(_order(self.store))
        stranger = User.objects.create_user("stranger")
        Store.objects.create(owner=stranger, name="Lain", slug="lain")
        client = Client()
        client.force_login(stranger)

        self.assertEqual(client.get(f"/merchant/invoices/{invoice.id}/").status_code, 404)
