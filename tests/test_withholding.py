"""PPh withheld from payments: ledger, bukti potong, register and deposit."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import controls, withholding
from apps.accounting.ledger import account_balances
from apps.accounting.models import Expense, JournalEntry, Supplier, Withholding
from apps.accounting.payables import PayableError, create_bill, pay_bill


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class WithholdingTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.supplier = Supplier.objects.create(store=self.store, name="Konsultan", npwp="0123456789012345")
        self.bill = create_bill(self.store, supplier=self.supplier, lines=[{"description": "Jasa", "qty": 1,
            "unit_price": Decimal(10_000_000)}], bill_date=self.today, tax=Decimal(1_100_000), category="1400")

    def test_compute_rates_and_rounding(self):
        self.assertEqual(withholding.compute("pph23_jasa", 10_000_000), (Decimal(2), Decimal(200_000)))
        self.assertEqual(withholding.compute("pph4_2_sewa", 1_000_000, "10")[1], Decimal(100_000))
        self.assertEqual(withholding.compute("pph21", 1_234_567, "2.5")[1], Decimal(30_864))
        for bad in (0, 51, -1):
            with self.assertRaises(withholding.WithholdingError):
                withholding.compute("pph23_jasa", 1000, bad)
        with self.assertRaises(withholding.WithholdingError):
            withholding.compute("nonsense", 1000)

    def test_paying_a_bill_with_pph_pays_the_bank_less_and_settles_the_payable_fully(self):
        before = balances(self.store)
        payment = pay_bill(self.bill, amount=Decimal(11_100_000), paid_on=self.today,
            withholding=("pph23_jasa", None), actor=self.owner)
        after = balances(self.store)
        # PPh 23 is on the part before PPN: 2% of 10.000.000
        self.assertEqual(payment.withheld, Decimal(200_000))
        self.assertEqual(after.get("2150", 0), Decimal(200_000))
        self.assertEqual(after.get("1100", 0) - before.get("1100", 0), Decimal(-10_900_000))
        self.assertEqual(after.get("2300", 0), 0)                      # the debt to the supplier is gone
        slip = Withholding.objects.get()
        self.assertEqual((slip.base, slip.rate, slip.amount, slip.payee_npwp),
            (Decimal(10_000_000), Decimal(2), Decimal(200_000), "0123456789012345"))
        self.assertTrue(slip.number.startswith("BP/"))
        self.assertEqual(self.bill.__class__.objects.get(pk=self.bill.pk).status, "paid")

    def test_partial_payment_withholds_in_proportion(self):
        payment = pay_bill(self.bill, amount=Decimal(5_550_000), paid_on=self.today, withholding=("pph23_jasa", None))
        self.assertEqual((Withholding.objects.get().base, payment.withheld), (Decimal(5_000_000), Decimal(100_000)))

    def test_bad_rate_pays_nothing(self):
        with self.assertRaises(PayableError):
            pay_bill(self.bill, amount=Decimal(1_000_000), paid_on=self.today, withholding=("pph23_jasa", "99"))
        self.assertEqual(self.bill.payments.count(), 0)
        self.assertEqual(Withholding.objects.count(), 0)

    def test_expense_with_pph_posts_gross_cost_net_cash_and_a_slip(self):
        before = balances(self.store)
        category = Expense._meta.get_field("category").choices[0][0]
        self.client.post(reverse("merchant-expenses"), {"date": self.today.isoformat(), "category": category,
            "description": "Sewa gudang", "vendor": "Pak Haji", "amount": "5000000", "pph_kind": "pph4_2_sewa"})
        expense = Expense.objects.get()
        self.assertEqual((expense.withheld, expense.amount), (Decimal(500_000), Decimal(4_500_000)))
        after = balances(self.store)
        self.assertEqual(after.get(category, 0) - before.get(category, 0), Decimal(5_000_000))   # the full cost
        self.assertEqual(after.get("1100", 0) - before.get("1100", 0), Decimal(-4_500_000))
        self.assertEqual(after.get("2150", 0), Decimal(500_000))
        slip = Withholding.objects.get()
        self.assertEqual((slip.payee_name, slip.kind, slip.amount), ("Pak Haji", "pph4_2_sewa", Decimal(500_000)))
        self.assertTrue(JournalEntry.objects.filter(expense=expense).exists())

    def test_deposit_clears_utang_pph_and_pays_from_the_bank(self):
        pay_bill(self.bill, amount=Decimal(11_100_000), paid_on=self.today, withholding=("pph23_jasa", None))
        slip = Withholding.objects.get()
        self.assertEqual(self.check().status, controls.OK)
        self.client.post(reverse("merchant-withholding-deposit"), {"ids": [slip.pk], "date": self.today.isoformat(),
            "reference": "NTPN123"})
        slip.refresh_from_db()
        self.assertEqual(slip.deposit.reference, "NTPN123")
        self.assertEqual(balances(self.store).get("2150", 0), 0)
        self.assertEqual(self.check().status, controls.OK)
        self.client.post(reverse("merchant-withholding-deposit"), {"ids": [slip.pk], "date": self.today.isoformat()})
        self.assertEqual(slip.deposit.__class__.objects.count(), 1)   # cannot be deposited twice

    def check(self):
        return next(c for c in controls.run_checks(self.store, self.today) if c.key == "withholding")

    def test_register_slip_and_csv(self):
        pay_bill(self.bill, amount=Decimal(11_100_000), paid_on=self.today, withholding=("pph23_jasa", None))
        slip = Withholding.objects.get()
        month = f"{self.today:%Y-%m}"
        page = self.client.get(reverse("merchant-withholdings"), {"month": month})
        self.assertContains(page, slip.number)
        self.assertContains(page, "Konsultan")
        csv = self.client.get(reverse("merchant-withholdings"), {"month": month, "format": "csv"}).content.decode()
        self.assertIn(slip.number, csv)
        self.assertIn("200000", csv)
        doc = self.client.get(reverse("merchant-withholding-slip", args=[slip.pk]))
        self.assertContains(doc, "BUKTI PEMOTONGAN PPH 23")
        self.assertContains(doc, "0123456789012345")

    def test_viewers_can_read_but_not_deposit(self):
        from django.contrib.auth.models import User

        from apps.shops.models import StoreStaff
        pay_bill(self.bill, amount=Decimal(11_100_000), paid_on=self.today, withholding=("pph23_jasa", None))
        viewer = User.objects.create_user("vw4", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-withholdings")).status_code, 200)
        self.assertEqual(self.client.post(reverse("merchant-withholding-deposit"),
            {"ids": [Withholding.objects.get().pk], "date": self.today.isoformat()}).status_code, 404)
