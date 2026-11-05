"""Biaya operasional: recorded expenses post to the ledger, show in profit and loss, and give
PKP sellers their PPN masukan."""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.accounting.ledger import CASH, INPUT_VAT, post_pending_entries
from apps.accounting.models import Expense, JournalEntry, JournalLine
from apps.orders.models import Order, PaymentTransaction
from apps.shops.models import AuditLog, Store, StoreStaff
from apps.tax.models import TaxProfile
from apps.tax.services import monthly_tax

MEDIA = tempfile.mkdtemp()


def _lines(entry):
    return {line.account.code: line.debit - line.credit for line in entry.lines.select_related("account")}


@override_settings(PLATFORM_DOMAIN="", MEDIA_ROOT=MEDIA)
class ExpenseTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user("expense-owner")
        self.store = Store.objects.create(owner=self.owner, name="Buku", slug="buku")
        self.client = Client()
        self.client.force_login(self.owner)
        self.today = timezone.localdate()

    def _record(self, **data):
        values = {"date": self.today.isoformat(), "category": "6300",
            "description": "Sewa ruko November", "amount": "2500000"}
        return self.client.post("/merchant/expenses/", {**values, **data})

    def test_expense_posts_to_ledger_and_profit(self):
        response = self._record(vendor="Pak Budi")
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(_lines(expense.journal_entry),
            {"6300": Decimal(2500000), CASH: Decimal(-2500000)})
        self.assertTrue(AuditLog.objects.filter(action="expense.created").exists())

        finance = self.client.get("/merchant/finance/")
        self.assertContains(finance, "Sewa tempat")
        self.assertEqual(finance.context["profit"]["net"], Decimal(-2500000))

    def test_pkp_input_vat_reduces_vat_payable(self):
        TaxProfile.objects.create(store=self.store, is_pkp=True)
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=Decimal(100000), tax=Decimal(11000),
            total=Decimal(111000), idempotency_key="exp-1")
        PaymentTransaction.objects.create(order=order, provider="demo", external_id="p1",
            status="succeeded", amount=order.total, reconciled=True)
        post_pending_entries()
        self._record(category="6400", description="Iklan", amount="1110000", input_vat="110000")

        expense = Expense.objects.get()
        self.assertEqual(_lines(expense.journal_entry), {"6400": Decimal(1000000),
            INPUT_VAT: Decimal(110000), CASH: Decimal(-1110000)})
        report = monthly_tax(self.store, self.today.year, self.today.month)
        self.assertEqual((report.vat, report.input_vat), (Decimal(11000), Decimal(110000)))
        self.assertEqual(report.vat_payable, Decimal(-99000))  # lebih bayar
        self.assertContains(self.client.get("/merchant/tax/"), "lebih bayar")

    def test_input_vat_is_ignored_for_non_pkp(self):
        self._record(input_vat="100000")
        self.assertEqual(Expense.objects.get().input_vat, 0)

    def test_validation(self):
        future = (self.today + timedelta(days=1)).isoformat()
        self.assertContains(self._record(date=future), "masa depan")
        self.assertContains(self._record(amount="0"), "lebih dari 0")
        TaxProfile.objects.create(store=self.store, is_pkp=True)
        self.assertContains(self._record(amount="1000", input_vat="1000"), "lebih kecil")
        page = self._record(receipt=SimpleUploadedFile("nota.html", b"<script>", "image/png"))
        self.assertContains(page, "Unggah foto")
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(JournalEntry.objects.exists())

    def test_receipt_is_private_and_served_to_staff_only(self):
        self._record(receipt=SimpleUploadedFile("nota.pdf", b"%PDF-1.4 nota", "application/pdf"))
        expense = Expense.objects.get()
        self.assertTrue(expense.receipt.name.startswith("expenses/buku/"))
        response = self.client.get(f"/merchant/expenses/{expense.id}/receipt/")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 nota")

        other = User.objects.create_user("other-seller")
        Store.objects.create(owner=other, name="Lain", slug="lain")
        stranger = Client()
        stranger.force_login(other)
        self.assertEqual(stranger.get(f"/merchant/expenses/{expense.id}/receipt/").status_code, 404)

    def test_delete_removes_journal_entry(self):
        self._record()
        expense = Expense.objects.get()
        self.client.post(f"/merchant/expenses/{expense.id}/delete/")
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(JournalLine.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="expense.removed").exists())

    def test_month_list_csv_and_copy(self):
        self._record()
        last_month = (self.today.replace(day=1) - timedelta(days=1))
        Expense.objects.create(store=self.store, date=last_month, category="6200",
            description="Gaji Oktober", amount=Decimal(3000000))
        page = self.client.get("/merchant/expenses/")
        self.assertContains(page, "Sewa ruko November")
        self.assertNotContains(page, "Gaji Oktober")
        self.assertEqual(page.context["total"], Decimal(2500000))

        csv = self.client.get(f"/merchant/expenses/?month={last_month:%Y-%m}&format=csv")
        self.assertIn("Gaji Oktober", csv.content.decode())

        source = Expense.objects.get(description="Gaji Oktober")
        copy = self.client.get(f"/merchant/expenses/?copy={source.id}")
        self.assertEqual(copy.context["form"].initial["description"], "Gaji Oktober")
        self.assertEqual(copy.context["form"].initial["date"], self.today)

    def test_viewer_sees_but_cannot_record(self):
        viewer = User.objects.create_user("expense-viewer")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        client = Client()
        client.force_login(viewer)
        page = client.get("/merchant/expenses/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Simpan biaya")
        response = client.post("/merchant/expenses/", {"date": self.today.isoformat(),
            "category": "6300", "description": "x", "amount": "1"})
        self.assertIn(response.status_code, {403, 404})
        self.assertFalse(Expense.objects.exists())
