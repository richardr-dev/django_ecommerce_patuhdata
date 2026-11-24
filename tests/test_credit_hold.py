"""Tahan kredit: no new credit sale to a customer with an invoice more than N days past due."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.shops.models import AuditLog, StoreStaff
from apps.tax.credit_limit import CreditHold, held_customers, overdue_hold
from apps.tax.models import Invoice, ReminderPolicy
from apps.tax.sales import create_sales_invoice
from apps.tax.services import record_payment


class CreditHoldTests(Base):
    def setUp(self):
        super().setUp()
        ReminderPolicy.objects.create(store=self.store, credit_hold_days=30)

    def sell(self, credit=True, check=True):
        return create_sales_invoice(self.store, buyer_name="Budi", customer=self.customer, on_credit=credit,
            lines=[{"product": self.kabel, "qty": 1, "unit_price": Decimal(100_000)}], credit_check=check)

    def late(self, days):
        invoice = self.sell()
        Invoice.objects.filter(pk=invoice.pk).update(due_date=self.today - timedelta(days=days))
        return Invoice.objects.get(pk=invoice.pk)

    def test_late_beyond_the_rule_holds_new_credit_but_not_cash(self):
        self.late(20)
        self.sell()  # 20 days late is inside the 30-day rule
        old = self.late(45)
        with self.assertRaisesMessage(CreditHold, f"invoice {old.number} terlambat 45 hari"):
            self.sell()
        self.sell(credit=False)  # cash still sells
        self.sell(check=False)  # automatic flows are never held
        self.assertEqual([c for c, _, _ in held_customers(self.store)], [self.customer])

    def test_paying_the_late_invoice_lifts_the_hold(self):
        old = self.late(45)
        record_payment(old, old.total, self.today, "transfer")
        self.assertIsNone(overdue_hold(self.customer))
        self.sell()

    def test_no_rule_no_hold(self):
        ReminderPolicy.objects.filter(store=self.store).update(credit_hold_days=None)
        self.late(400)
        self.sell()
        self.assertEqual(held_customers(self.store), [])

    def test_form_refuses_a_clerk_and_logs_a_manager_override(self):
        old = self.late(45)
        clerk, boss = User.objects.create_user("ar"), User.objects.create_user("mgr")
        StoreStaff.objects.create(store=self.store, user=clerk, role="ar_staff")
        StoreStaff.objects.create(store=self.store, user=boss, role="manager")
        data = {"customer_id": str(self.customer.id), "buyer_name": "Budi", "payment_status": "credit",
            "issued_on": self.today.isoformat(), "product[]": [str(self.kabel.pk)], "description[]": ["Kabel"],
            "qty[]": ["1"], "unit_price[]": ["100000"], "credit_override": "1"}
        self.client.force_login(clerk)
        self.assertContains(self.client.post(reverse("merchant-invoice-new"), {**data, "idem": "c1"}), "Kredit Budi ditahan")
        self.client.force_login(boss)
        self.client.post(reverse("merchant-invoice-new"), {**data, "idem": "c2"})
        invoice = Invoice.objects.exclude(pk=old.pk).get()
        self.assertTrue(AuditLog.objects.filter(action="invoice.credit_hold_override", object_id=str(invoice.pk),
            actor=boss).exists())

    def test_policy_page_and_customer_page(self):
        self.late(45)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-receivables"))
        self.assertContains(page, "Tahan kredit · 1 pelanggan")
        found = self.client.get(reverse("merchant-customer-search") + "?q=budi").json()["results"][0]
        self.assertIn("terlambat 45 hari", found["credit_hold"])  # the invoice form warns before saving
        self.client.post(reverse("merchant-credit-hold-policy"), {"hold": "1", "credit_hold_days": "60"})
        self.assertEqual(ReminderPolicy.objects.get(store=self.store).credit_hold_days, 60)
        self.assertIsNone(overdue_hold(self.customer))
        self.client.post(reverse("merchant-credit-hold-policy"), {"hold": "1", "credit_hold_days": "7"})
        detail = self.client.get(reverse("merchant-customer-detail", args=[self.customer.id]))
        self.assertContains(detail, "Kredit ditahan")
        self.client.post(reverse("merchant-credit-hold-policy"), {"credit_hold_days": "7"})  # unticked: off
        self.assertIsNone(ReminderPolicy.objects.get(store=self.store).credit_hold_days)
