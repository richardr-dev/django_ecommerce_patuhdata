"""Batas kredit: a credit sale past the customer's limit is refused unless a manager overrides."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.shops.models import AuditLog, StoreStaff
from apps.tax.credit_limit import CreditLimitExceeded, position
from apps.tax.models import Invoice
from apps.tax.sales import create_sales_invoice, invoice_lines, update_sales_invoice
from apps.tax.services import record_payment


class CreditLimitServiceTests(Base):
    def setUp(self):
        super().setUp()
        self.customer.credit_limit = Decimal(300_000)
        self.customer.save()

    def sell(self, kabel=1, credit=True, check=True):
        return create_sales_invoice(self.store, buyer_name="Budi", customer=self.customer, on_credit=credit,
            lines=[{"product": self.kabel, "qty": kabel, "unit_price": Decimal(100_000)}], credit_check=check)

    def test_refused_past_the_limit_and_nothing_is_booked(self):
        self.sell(kabel=2)  # 222.000 with PPN
        with self.assertRaisesMessage(CreditLimitExceeded, "Melewati batas kredit Budi"):
            self.sell(kabel=1)  # +111.000 > 300.000
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(self.on_hand(self.kabel), 98)

    def test_payment_frees_the_limit(self):
        first = self.sell(kabel=2)
        record_payment(first, Decimal(150_000), self.today, "transfer")
        self.sell(kabel=1)  # owed 72.000 + 111.000
        self.assertEqual(position(self.customer).owed, Decimal(183_000))

    def test_cash_sales_automatic_flows_and_no_limit_are_not_checked(self):
        self.sell(kabel=5, credit=False)  # paid now: nothing is owed
        self.sell(kabel=5, check=False)  # an automatic flow does not ask
        self.customer.credit_limit = None
        self.customer.save()
        self.sell(kabel=5)
        self.assertEqual(Invoice.objects.count(), 3)

    def test_zero_means_cash_only(self):
        self.customer.credit_limit = Decimal(0)
        self.customer.save()
        with self.assertRaisesMessage(CreditLimitExceeded, "hanya boleh membeli tunai"):
            self.sell(kabel=1)

    def test_raising_an_invoice_by_editing_it_is_checked_too(self):
        invoice = self.sell(kabel=2)
        lines = invoice_lines(invoice)
        lines[0]["qty"] = 3
        with self.assertRaises(CreditLimitExceeded):
            update_sales_invoice(invoice, buyer_name="Budi", issued_on=invoice.issued_on,
                due_date=invoice.due_date, notes="", customer=self.customer, lines=lines, credit_check=True)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, Decimal(222_000))


class CreditLimitPageTests(Base):
    def setUp(self):
        super().setUp()
        self.customer.credit_limit = Decimal(100_000)
        self.customer.save()

    def post(self, user, **extra):
        self.client.force_login(user)
        data = {"idem": f"k-{user.pk}-{len(extra)}", "customer_id": str(self.customer.id), "buyer_name": "Budi",
            "payment_status": "credit", "issued_on": self.today.isoformat(), "product[]": [str(self.kabel.pk)],
            "description[]": ["Kabel"], "qty[]": ["2"], "unit_price[]": ["100000"]}
        data.update(extra)
        return self.client.post(reverse("merchant-invoice-new"), data)

    def test_sales_staff_are_refused_and_a_manager_may_override(self):
        clerk, boss = User.objects.create_user("ar"), User.objects.create_user("mgr")
        StoreStaff.objects.create(store=self.store, user=clerk, role="ar_staff")
        StoreStaff.objects.create(store=self.store, user=boss, role="manager")

        refused = self.post(clerk, credit_override="1")  # not theirs to give
        self.assertContains(refused, "Melewati batas kredit")
        self.assertFalse(Invoice.objects.exists())

        self.post(boss, credit_override="1")
        invoice = Invoice.objects.get()
        self.assertTrue(AuditLog.objects.filter(action="invoice.credit_limit_override",
            object_id=str(invoice.pk), actor=boss).exists())

    def test_only_sales_credit_holders_set_the_limit(self):
        clerk = User.objects.create_user("cs")  # may edit customers, not their credit
        StoreStaff.objects.create(store=self.store, user=clerk, role="customer_service")
        url = reverse("merchant-customer-terms", args=[self.customer.id])
        self.client.force_login(clerk)
        self.client.post(url, {"payment_terms_days": "30", "credit_limit": "9.000.000"})
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.payment_terms_days, self.customer.credit_limit), (30, Decimal(100_000)))

        self.client.force_login(self.owner)
        self.client.post(url, {"payment_terms_days": "30", "credit_limit": "9.000.000"})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.credit_limit, Decimal(9_000_000))
        page = self.client.get(reverse("merchant-customer-detail", args=[self.customer.id]))
        self.assertContains(page, "Batas kredit")
