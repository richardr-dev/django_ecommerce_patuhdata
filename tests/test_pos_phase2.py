"""Kasir (POS) phase 2: void and refund at the register, paid-in/paid-out, and approval of a shift
variance above the store's threshold. See docs/pos-architecture.md §8, phase 2."""
from decimal import Decimal

from django.contrib.auth.models import User
from test_pos import PosBase

from apps.accounting import approvals
from apps.accounting.ledger import CASH_SHORT, account_balances
from apps.accounting.models import ApprovalPolicy, Expense, JournalEntry
from apps.pos import services as pos
from apps.pos.models import PosSale
from apps.shops.models import StoreStaff
from apps.tax.models import CreditNote, Invoice


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class VoidTests(PosBase):
    def setUp(self):
        super().setUp()
        self.shift = self.open()

    def test_cashier_can_void_their_own_open_shift_sale(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 2}], tenders=[{"method": "cash", "amount": 200_000}])
        before = pos.drawer_balance(self.register)
        voided = pos.void_sale(sale, actor=self.kasir, reason="salah input")
        self.assertEqual(voided.status, PosSale.VOID)
        self.assertEqual(voided.void_reason, "salah input")
        voided.invoice.refresh_from_db()
        self.assertEqual(voided.invoice.status, Invoice.STATUS_VOID)
        self.assertEqual(self.shelf(self.outlet, self.kabel), 20)              # stock is back
        self.assertEqual(pos.drawer_balance(self.register), before - 200_000)  # cash reversed

    def test_a_cashier_cannot_void_another_cashiers_sale(self):
        other = User.objects.create_user("kasir2", password="x")
        StoreStaff.objects.create(store=self.store, user=other, role="cashier", outlet=self.outlet)
        sale = self.sell(cashier=other)
        with self.assertRaises(pos.PosError) as caught:
            pos.void_sale(sale, actor=self.kasir)
        self.assertIn("struk Anda sendiri", str(caught.exception))

    def test_a_manager_can_void_anyones_sale(self):
        sale = self.sell()
        pos.void_sale(sale, actor=self.owner)
        self.assertEqual(PosSale.objects.get(pk=sale.pk).status, PosSale.VOID)

    def test_voiding_after_the_shift_closes_is_refused(self):
        sale = self.sell()
        pos.close_shift(self.shift, counted_cash=pos.drawer_balance(self.register), actor=self.kasir)
        with self.assertRaises(pos.PosError) as caught:
            pos.void_sale(sale, actor=self.kasir)
        self.assertIn("sudah ditutup", str(caught.exception))

    def test_voiding_twice_is_refused(self):
        sale = self.sell()
        pos.void_sale(sale, actor=self.kasir)
        with self.assertRaises(pos.PosError):
            pos.void_sale(sale, actor=self.kasir)


class RefundTests(PosBase):
    def setUp(self):
        super().setUp()
        self.shift = self.open()

    def test_refund_restocks_and_pays_from_the_drawer(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 2}], tenders=[{"method": "cash", "amount": 200_000}])
        item = sale.invoice.items.get()
        before = pos.drawer_balance(self.register)
        note = pos.refund_sale(sale, actor=self.owner, lines=[{"item": item.pk, "quantity": 1, "restock": True}],
            reason="ukuran salah")
        self.assertEqual(note.excess_action, CreditNote.EXCESS_REFUND)
        self.assertEqual(note.bank_account, self.register.cash_account)
        self.assertEqual(self.shelf(self.outlet, self.kabel), 19)              # 20 - 2 + 1 back
        self.assertEqual(pos.drawer_balance(self.register), before - note.total)

    def test_refund_without_restock_leaves_the_shelf_short(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 1}])
        item = sale.invoice.items.get()
        pos.refund_sale(sale, actor=self.owner, lines=[{"item": item.pk, "quantity": 1, "restock": False}])
        self.assertEqual(self.shelf(self.outlet, self.kabel), 19)              # damaged: stays out

    def test_a_cashier_cannot_refund(self):
        sale = self.sell()
        item = sale.invoice.items.get()
        with self.assertRaises(pos.PosError) as caught:
            pos.refund_sale(sale, actor=self.kasir, lines=[{"item": item.pk, "quantity": 1, "restock": True}])
        self.assertIn("tidak berhak", str(caught.exception))

    def test_cannot_refund_more_than_was_sold(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 1}])
        item = sale.invoice.items.get()
        with self.assertRaises(pos.PosError):
            pos.refund_sale(sale, actor=self.owner, lines=[{"item": item.pk, "quantity": 5, "restock": True}])


class PaidInOutTests(PosBase):
    def setUp(self):
        super().setUp()
        self.shift = self.open()

    def test_paid_out_reduces_the_drawer_and_books_an_expense(self):
        before = pos.drawer_balance(self.register)
        expense = pos.record_paid_out(self.shift, amount=15_000, reason="parkir", actor=self.kasir)
        self.assertEqual(expense.category, "6900")
        self.assertEqual(expense.bank_account, self.register.cash_account)
        self.assertEqual(pos.drawer_balance(self.register), before - 15_000)
        self.assertEqual(Expense.objects.filter(pk=expense.pk, store=self.store).count(), 1)

    def test_paid_out_needs_a_positive_amount(self):
        with self.assertRaises(pos.PosError):
            pos.record_paid_out(self.shift, amount=0, reason="x", actor=self.kasir)

    def test_paid_in_tops_up_the_drawer_from_a_named_account(self):
        before, main_before = pos.drawer_balance(self.register), balance(self.store, self.main.account.code)
        pos.record_paid_in(self.shift, amount=50_000, fund_from=self.main, reason="tambahan modal", actor=self.owner)
        self.assertEqual(pos.drawer_balance(self.register), before + 50_000)
        self.assertEqual(balance(self.store, self.main.account.code), main_before - 50_000)

    def test_a_cashier_cannot_add_paid_in(self):
        with self.assertRaises(pos.PosError):
            pos.record_paid_in(self.shift, amount=50_000, fund_from=self.main, reason="x", actor=self.kasir)

    def test_paid_out_needs_an_open_shift(self):
        pos.close_shift(self.shift, counted_cash=pos.drawer_balance(self.register), actor=self.kasir)
        with self.assertRaises(pos.PosError):
            pos.record_paid_out(self.shift, amount=10_000, reason="x", actor=self.kasir)


class ShiftVarianceApprovalTests(PosBase):
    def enable_policy(self, threshold=10_000):
        ApprovalPolicy.objects.update_or_create(store=self.store, kind="pos_shift_variance",
            defaults={"enabled": True, "threshold": threshold})

    def test_a_variance_above_the_threshold_waits_for_approval(self):
        self.enable_policy(threshold=10_000)
        shift = self.open()
        shift = pos.close_shift(shift, counted_cash=pos.drawer_balance(self.register) - 20_000, actor=self.kasir)
        self.assertTrue(shift.variance_pending_approval)
        self.assertIsNotNone(shift.variance_request)
        self.assertEqual(JournalEntry.objects.filter(source_type="pos_shift", source_id=str(shift.pk)).count(), 0)
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(0))
        approvals.approve(shift.variance_request, self.owner)
        shift.refresh_from_db()
        self.assertFalse(shift.variance_pending_approval)
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(20_000))
        self.assertEqual(JournalEntry.objects.filter(source_type="pos_shift", source_id=str(shift.pk)).count(), 1)

    def test_a_variance_below_the_threshold_still_posts_immediately(self):
        self.enable_policy(threshold=10_000)
        shift = self.open()
        shift = pos.close_shift(shift, counted_cash=pos.drawer_balance(self.register) - 5_000, actor=self.kasir)
        self.assertFalse(shift.variance_pending_approval)
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(5_000))

    def test_rejecting_leaves_the_variance_unposted_and_out_of_the_backlog(self):
        self.enable_policy(threshold=10_000)
        shift = self.open()
        shift = pos.close_shift(shift, counted_cash=pos.drawer_balance(self.register) - 20_000, actor=self.kasir)
        request = shift.variance_request
        approvals.reject(request, self.owner, note="hitung ulang dulu")
        shift.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(request.status, request.REJECTED)
        self.assertTrue(shift.variance_pending_approval)                       # never auto-posted by the backlog
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(0))
        pos.post_unposted_shift_variances()
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(0))          # still not booked

    def test_the_closer_cannot_approve_their_own_shift(self):
        self.enable_policy(threshold=10_000)
        shift = self.open()
        shift = pos.close_shift(shift, counted_cash=pos.drawer_balance(self.register) - 20_000, actor=self.kasir)
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(shift.variance_request, self.kasir)
