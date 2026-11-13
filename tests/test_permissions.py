"""Hak akses: one matrix, enforced everywhere; the accountant never writes; a maker cannot check."""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import receipts
from apps.accounting.models import Supplier
from apps.accounting.payables import create_bill
from apps.shops import permissions
from apps.shops.models import StoreStaff

# The matrix exactly as it was when the role sets were still scattered, so centralising changed nothing.
EXPECTED = {
    "books.view": {"admin", "manager", "finance", "viewer"}, "books.edit": {"admin", "finance"},
    "approval.decide": {"admin", "manager", "finance"}, "period.close": {"admin", "finance"},
    "period.reopen": {"admin"}, "intercompany.create": {"admin"},
    "tax.view": {"admin", "manager", "finance", "viewer"}, "tax.edit": {"admin", "finance"},
    "sales.view": {"admin", "manager", "ops", "finance", "viewer"}, "sales.edit": {"admin", "manager", "ops", "finance"},
    "orders.manage": {"admin", "manager", "ops", "customer_service"}, "giftcards.manage": {"admin", "manager", "customer_service"},
    "settlement.manage": {"admin", "manager", "finance"}, "settlement.view": {"admin", "manager", "finance", "viewer"},
    "stock.fulfil": {"admin", "manager", "ops", "warehouse"}, "stock.receive": {"admin", "manager", "finance", "warehouse"},
    "stock.oversell": {"admin", "manager"},
    "stock.request": {"admin", "manager", "ops", "warehouse"},
    "stock.request_approve": {"admin", "manager"},
    "stock.send": {"admin", "manager", "ops", "warehouse"},
    "stock.count": {"admin", "manager", "ops", "warehouse"},
    "stock.count_approve": {"admin", "manager", "finance"},
    "purchase.order": {"admin", "manager", "finance", "ops"},
    "customers.view": {"admin", "manager", "ops", "customer_service", "viewer"},
    "customers.edit": {"admin", "manager", "customer_service"}, "customers.broadcast": {"admin", "manager"},
    "channels.manage": {"admin", "manager", "ops"}, "channels.mapping": {"admin", "manager", "ops"},
    "channels.income": {"admin", "finance"}, "reports.view": {"admin", "manager", "finance", "ops", "viewer"},
    "recap.view": {"admin", "manager", "ops", "warehouse", "customer_service", "viewer"},
    "settings.manage": {"admin", "manager"}, "billing.manage": {"admin", "finance"}, "staff.manage": {"admin"},
}


class MatrixTests(Base):
    def test_the_matrix_is_pinned(self):
        """Giving a role a new power means changing this test on purpose."""
        self.assertEqual({k: set(v[2]) for k, v in permissions.MATRIX.items()}, EXPECTED)

    def test_only_known_roles_appear(self):
        known = set(permissions.ROLE_LABELS)
        for _group, _label, allowed in permissions.MATRIX.values():
            self.assertLessEqual(allowed, known - {"accountant"})   # the accountant is granted reads, never listed

    def test_modules_take_their_roles_from_the_matrix(self):
        from apps.accounting import approvals, views, views_books
        from apps.tax import sales_views
        self.assertEqual(views.EDIT_ROLES, permissions.roles("books.edit"))
        self.assertEqual(views_books.VIEW_ROLES, permissions.roles("books.view"))
        self.assertEqual(approvals.APPROVER_ROLES, permissions.roles("approval.decide"))
        self.assertEqual(sales_views.EDIT_ROLES, permissions.roles("sales.edit"))

    def test_no_view_module_declares_its_own_role_set(self):
        pattern = re.compile(r"^[A-Z_]*ROLES\s*=\s*\{", re.MULTILINE)
        offenders = [str(p) for p in Path("apps").rglob("*.py") if "migrations" not in str(p)
            and pattern.search(p.read_text()) and p.name != "permissions.py"]
        self.assertEqual(offenders, [])

    def test_every_merchant_view_checks_a_role(self):
        marker = re.compile(r"_require_roles|_store\(request|_staff_store|_can_edit|_has_role|_can_manage|ROLES|roles")
        missing = []
        for path in Path("apps").rglob("*.py"):
            if "migrations" in str(path) or "test" in str(path):
                continue
            source = path.read_text()
            for m in re.finditer(r"@login_required[^\n]*\n(?:@[^\n]*\n)*def (merchant_\w+)\(request[^)]*\):\n((?:(?:    .*)?\n){0,60})", source):
                if not marker.search(m.group(2)):
                    missing.append(f"{path}:{m.group(1)}")
        self.assertEqual(missing, [])


class AccountantTests(Base):
    def setUp(self):
        super().setUp()
        self.auditor = User.objects.create_user("auditor", password="x")
        StoreStaff.objects.create(store=self.store, user=self.auditor, role="accountant")
        self.client.force_login(self.auditor)

    def test_the_accountant_reads_but_never_writes(self):
        self.assertEqual(self.client.get(reverse("merchant-profit-loss")).status_code, 200)
        for name in ["merchant-bill-new", "merchant-journal-new", "merchant-goods-receipt-new",
                "merchant-year-close", "merchant-intercompany-new", "merchant-expenses"]:
            self.assertEqual(self.client.post(reverse(name), {"year": "2020"}).status_code, 404, name)
        supplier = Supplier.objects.create(store=self.store, name="S")
        receipt = receipts.create_receipt(self.store, supplier=supplier, received_on=self.today,
            lines=[{"sku": "KBL", "qty": Decimal(1), "unit_cost": Decimal(10)}])
        self.assertEqual(self.client.post(reverse("merchant-goods-receipt", args=[receipt.pk]),
            {"bill_date": self.today.isoformat()}).status_code, 404)


class SegregationTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT S")
        self.maker = User.objects.create_user("maker", password="x")
        StoreStaff.objects.create(store=self.store, user=self.maker, role="finance")
        self.bill = create_bill(self.store, supplier=self.supplier, bill_date=self.today,
            due_date=self.today + timedelta(days=7), actor=self.maker,
            lines=[{"description": "Jasa", "qty": 1, "unit_price": Decimal(100_000)}], category="6200")

    def pay(self, user):
        self.client.force_login(user)
        self.client.post(reverse("merchant-bill-detail", args=[self.bill.pk]),
            {"action": "pay", "amount": "100000", "paid_on": self.today.isoformat(), "method": "transfer"})
        self.bill.refresh_from_db()
        return self.bill.status

    def test_the_maker_cannot_pay_their_own_bill_when_someone_else_can(self):
        self.assertEqual(self.pay(self.maker), "open")          # the owner could pay it instead

    def test_another_person_can_pay_it(self):
        self.assertEqual(self.pay(self.owner), "paid")

    def test_a_one_person_store_is_never_locked_out(self):
        StoreStaff.objects.filter(user=self.maker).delete()
        owned = create_bill(self.store, supplier=self.supplier, bill_date=self.today, actor=self.owner,
            lines=[{"description": "Jasa", "qty": 1, "unit_price": Decimal(50_000)}], category="6200")
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-bill-detail", args=[owned.pk]),
            {"action": "pay", "amount": "50000", "paid_on": self.today.isoformat(), "method": "transfer"})
        owned.refresh_from_db()
        self.assertEqual(owned.status, "paid")

    def test_helper_rules(self):
        store = self.store
        self.assertTrue(permissions.maker_may_not_check(store, self.maker, self.maker))
        self.assertFalse(permissions.maker_may_not_check(store, self.owner, self.maker))
        self.assertFalse(permissions.maker_may_not_check(store, self.maker, None))


class AccessPageTests(Base):
    def test_admin_sees_the_matrix_and_others_do_not(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-access"))
        self.assertContains(page, "Matriks peran")
        self.assertContains(page, "Mencatat tagihan")
        finance = User.objects.create_user("fin", password="x")
        StoreStaff.objects.create(store=self.store, user=finance, role="finance")
        self.client.force_login(finance)
        self.assertEqual(self.client.get(reverse("merchant-access")).status_code, 404)
