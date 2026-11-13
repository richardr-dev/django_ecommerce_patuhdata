"""Maker and checker: above a threshold another person approves; nobody approves their own."""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import approvals
from apps.accounting.ledger import chart_of_accounts
from apps.accounting.models import (
    ApprovalPolicy,
    ApprovalRequest,
    Bill,
    Expense,
    JournalEntry,
    Supplier,
)
from apps.accounting.payables import create_bill
from apps.shops.models import StoreStaff


class ApprovalTests(Base):
    def setUp(self):
        super().setUp()
        self.finance = User.objects.create_user("fin", password="x")
        StoreStaff.objects.create(store=self.store, user=self.finance, role="finance")
        self.clerk = User.objects.create_user("clerk", password="x")
        StoreStaff.objects.create(store=self.store, user=self.clerk, role="finance")
        chart_of_accounts(self.store)
        self.cost = Expense._meta.get_field("category").choices[0][0]
        self.manager = User.objects.create_user("mgr", password="x")
        StoreStaff.objects.create(store=self.store, user=self.manager, role="manager")
        for kind in ("bill_payment", "expense", "journal", "invoice_void", "bill_void"):
            ApprovalPolicy.objects.create(store=self.store, kind=kind, threshold=1_000_000, enabled=True)
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        self.bill = create_bill(self.store, supplier=supplier, lines=[{"description": "x", "qty": 1,
            "unit_price": Decimal(5_000_000)}], bill_date=self.today)
        self.as_clerk()

    def as_clerk(self):
        self.client.force_login(self.clerk)

    def pay(self, amount):
        return self.client.post(reverse("merchant-bill-detail", args=[self.bill.pk]), {"action": "pay",
            "amount": str(amount), "paid_on": self.today.isoformat()})

    def test_small_payments_go_straight_through_large_ones_wait(self):
        self.pay(500_000)
        self.assertEqual(self.bill.payments.count(), 1)
        self.pay(2_000_000)
        self.assertEqual(self.bill.payments.count(), 1)                 # parked, not paid
        request = ApprovalRequest.objects.get()
        self.assertEqual((request.kind, request.amount, request.status), ("bill_payment", Decimal(2_000_000), "pending"))
        self.assertEqual(request.requester, self.clerk)

    def test_the_maker_cannot_approve_but_another_person_can_and_it_runs_as_the_maker(self):
        self.pay(2_000_000)
        request = ApprovalRequest.objects.get()
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(request, self.clerk)
        self.client.post(reverse("merchant-approval-decide", args=[request.pk]), {"decision": "approve"})
        request.refresh_from_db()
        self.assertEqual((request.status, request.decided_by), ("pending", None))          # the maker is refused
        self.client.force_login(self.finance)
        self.client.post(reverse("merchant-approval-decide", args=[request.pk]), {"decision": "approve"})
        request.refresh_from_db()
        self.assertEqual((request.status, request.decided_by), ("approved", self.finance))
        payment = self.bill.payments.get()
        self.assertEqual((payment.amount, payment.recorded_by), (Decimal(2_000_000), self.clerk))
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(request, self.finance)                    # cannot run twice

    def test_rejecting_pays_nothing_and_keeps_a_note(self):
        self.pay(2_000_000)
        request = ApprovalRequest.objects.get()
        self.client.force_login(self.finance)
        self.client.post(reverse("merchant-approval-decide", args=[request.pk]), {"decision": "reject", "note": "Salah supplier"})
        request.refresh_from_db()
        self.assertEqual((request.status, request.decision_note), ("rejected", "Salah supplier"))
        self.assertEqual(self.bill.payments.count(), 0)

    def test_an_expense_is_off_the_books_until_approved(self):
        data = {"date": self.today.isoformat(), "category": self.cost, "description": "Sewa ruko", "amount": "3000000"}
        self.client.post(reverse("merchant-expenses"), data)
        expense = Expense.objects.get()
        self.assertTrue(expense.approval_pending)
        self.assertFalse(JournalEntry.objects.filter(source_type="expense", source_id=str(expense.pk)).exists()
            or JournalEntry.objects.filter(expense=expense).exists())
        self.assertEqual(self.client.get(reverse("merchant-expenses")).context["expenses"].count(), 0)
        request = ApprovalRequest.objects.get()
        approvals.approve(request, self.finance)
        expense.refresh_from_db()
        self.assertFalse(expense.approval_pending)
        self.assertTrue(JournalEntry.objects.filter(expense=expense).exists())

    def test_a_rejected_expense_disappears(self):
        self.client.post(reverse("merchant-expenses"), {"date": self.today.isoformat(), "category": self.cost,
            "description": "Aneh", "amount": "9000000"})
        approvals.reject(ApprovalRequest.objects.get(), self.finance, "Tidak jelas")
        self.assertEqual(Expense.objects.count(), 0)

    def test_manual_journal_and_voids_wait_too(self):
        self.client.post(reverse("merchant-journal-new"), {"date": self.today.isoformat(), "memo": "Koreksi besar",
            "account[]": [self.cost, "1100"], "debit[]": ["4000000", "0"], "credit[]": ["0", "4000000"]})
        self.assertEqual(ApprovalRequest.objects.filter(kind="journal").count(), 1)
        self.assertFalse(JournalEntry.objects.filter(memo="Koreksi besar").exists())
        invoice = self.invoice(kabel=20)                                 # well above the threshold
        self.client.force_login(self.manager)                            # the one who may void invoices
        self.client.post(reverse("merchant-invoice-detail", args=[invoice.pk]), {"action": "void"})
        self.as_clerk()
        invoice.refresh_from_db()
        self.assertNotEqual(invoice.status, "void")
        self.client.post(reverse("merchant-bill-detail", args=[self.bill.pk]), {"action": "void"})
        self.bill.refresh_from_db()
        self.assertNotEqual(self.bill.status, Bill.STATUS_VOID)
        for request in ApprovalRequest.objects.filter(status="pending"):
            approvals.approve(request, self.finance)
        invoice.refresh_from_db()
        self.bill.refresh_from_db()
        self.assertEqual(invoice.status, "void")
        self.assertEqual(self.bill.status, Bill.STATUS_VOID)
        self.assertTrue(JournalEntry.objects.filter(memo="Koreksi besar").exists())

    def test_a_store_with_nobody_else_to_approve_is_never_blocked(self):
        StoreStaff.objects.filter(store=self.store).delete()
        self.client.force_login(self.owner)
        self.assertFalse(approvals.required(self.store, "bill_payment", 9_000_000, self.owner))
        self.client.post(reverse("merchant-bill-detail", args=[self.bill.pk]), {"action": "pay",
            "amount": "2000000", "paid_on": self.today.isoformat()})
        self.assertEqual(self.bill.payments.count(), 1)

    def test_page_lists_pending_and_the_policy_is_admin_only(self):
        self.pay(2_000_000)
        page = self.client.get(reverse("merchant-approvals"))
        self.assertContains(page, "Bayar tagihan")
        self.assertContains(page, "Ini permintaan Anda")
        self.client.post(reverse("merchant-approvals"), {"on_expense": "1", "min_expense": "1"})
        self.assertEqual(ApprovalPolicy.objects.get(store=self.store, kind="expense").threshold, Decimal(1_000_000))  # clerk cannot
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-approvals"), {"on_expense": "1", "min_expense": "250.000"})
        self.assertEqual(ApprovalPolicy.objects.get(store=self.store, kind="expense").threshold, Decimal(250_000))
        self.assertFalse(ApprovalPolicy.objects.get(store=self.store, kind="journal").enabled)

    def test_pending_count_badge_for_approvers_only(self):
        self.pay(2_000_000)
        self.assertEqual(approvals.pending_count(self.store, self.clerk), 0)      # their own request
        self.assertEqual(approvals.pending_count(self.store, self.finance), 1)
        self.client.force_login(self.finance)
        page = self.client.get(reverse("merchant-dashboard"))
        self.assertIn("permintaan menunggu persetujuan Anda", [i["label"] for i in page.context["finance_todo"]])

    def test_the_history_page_records_who_asked_and_who_approved(self):
        self.pay(2_000_000)
        approvals.approve(ApprovalRequest.objects.get(), self.finance)
        from apps.shops.audit import trail
        what = [r["what"] for r in trail(self.store, self.bill)]
        self.assertIn("Diajukan untuk persetujuan", what)
        self.assertIn("Pembayaran tagihan", what)
        self.assertEqual(datetime.date.today(), self.today)
