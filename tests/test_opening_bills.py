"""Saldo awal utang per tagihan: old unpaid supplier bills become real bills that book no purchase."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import opening
from apps.accounting import opening_bills as service
from apps.accounting.controls import backdated_entries, payable
from apps.accounting.ledger import (
    EQUITY,
    INPUT_VAT,
    INVENTORY,
    PAYABLE,
    account_balances,
    live_entry,
)
from apps.accounting.models import Bill, Supplier
from apps.accounting.payables import aging, pay_bill, void_bill
from apps.accounting.supplier_notes import NoteError, create_debit_note


class OpeningBillTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Sumber Makmur")

    def carry(self, amount=4_800_000, number="FK-2210", days_ago=50, due_days=30, supplier=None):
        dated = self.today - timedelta(days=days_ago)
        return service.create_opening_bill(self.store, supplier=supplier or self.supplier, number=number,
            bill_date=dated, due_date=dated + timedelta(days=due_days), amount=Decimal(amount), actor=self.owner)

    def ledger(self):
        return {a.code: a.balance for a in account_balances(self.store)}

    def test_books_only_owner_equity_against_payable(self):
        bill = self.carry()
        entry = live_entry(self.store, "bill", bill.pk)
        lines = {line.account.code: (line.debit, line.credit) for line in entry.lines.select_related("account")}
        self.assertEqual(lines, {EQUITY: (Decimal(4_800_000), 0), PAYABLE: (0, Decimal(4_800_000))})
        self.assertEqual((balance(self.store, INVENTORY), balance(self.store, INPUT_VAT)), (0, 0))
        self.assertEqual((bill.is_opening, bill.tax, bill.status), (True, 0, Bill.STATUS_OVERDUE))
        self.assertEqual(aging(self.store, self.today)[1]["total"], Decimal(4_800_000))

    def test_posts_on_the_opening_date_and_refuses_a_later_bill(self):
        start = self.today - timedelta(days=10)
        opening.post_opening(self.store, date=start, banks={self.main: Decimal(10_000_000)})
        bill = self.carry()
        self.assertEqual(live_entry(self.store, "bill", bill.pk).date, start)
        with self.assertRaisesMessage(service.OpeningBillError, "pembelian baru"):
            self.carry(number="FK-2299", days_ago=2)

    def test_paid_like_any_bill_and_tied(self):
        bill = self.carry()
        pay_bill(bill, amount=Decimal(1_800_000), paid_on=self.today, bank_account=self.main, actor=self.owner)
        bill.refresh_from_db()
        self.assertEqual((bill.status, bill.amount_due), (Bill.STATUS_PARTIAL, Decimal(3_000_000)))
        self.assertEqual(balance(self.store, PAYABLE), Decimal(3_000_000))
        self.assertEqual(payable(self.store, self.today, self.ledger()).status, "ok")
        self.assertEqual(backdated_entries(self.store, self.today, self.ledger()).status, "ok")

    def test_no_debit_note_void_reverses_and_same_number_refused(self):
        bill = self.carry()
        with self.assertRaisesMessage(service.OpeningBillError, "sudah dicatat"):
            self.carry()
        with self.assertRaisesMessage(NoteError, "saldo awal"):
            create_debit_note(bill, quantities={bill.items.get().pk: 1}, reason="x", note_date=self.today)
        void_bill(bill)
        self.assertEqual(balance(self.store, PAYABLE), 0)
        self.carry()  # a voided one can be entered again

    def test_paste_creates_suppliers_and_is_all_or_nothing(self):
        text = ("Supplier\tNo\tTanggal\tJatuh tempo\tSisa\n"
                "pt sumber makmur\tFK-1\t01/09/2026\t01/10/2026\t1.250.000,00\n"
                "UD Plastik Jaya;SJ-118;2026-09-05;;650000\n")
        created = service.import_rows(self.store, service.parse(text), actor=self.owner)
        self.assertEqual([b.total for b in created], [Decimal(1_250_000), Decimal(650_000)])
        self.assertEqual(created[0].supplier, self.supplier)
        self.assertTrue(Supplier.objects.filter(store=self.store, name="UD Plastik Jaya").exists())
        bad = "CV A;X-1;01/09/2026;;100000\nCV B;X-2;01/09/2026;;nol"
        with self.assertRaisesMessage(service.OpeningBillError, "Baris 2"):
            service.import_rows(self.store, service.parse(bad), actor=self.owner)
        self.assertFalse(Bill.objects.filter(number="X-1").exists())

    def test_page_adds_one_and_lists_it(self):
        self.client.force_login(self.owner)
        url = reverse("merchant-opening-bills")
        response = self.client.post(url, {"supplier": "CV Baru", "number": "OLD-9", "amount": "300.000",
            "bill_date": (self.today - timedelta(days=60)).isoformat(), "idem": "b1"}, follow=True)
        self.assertContains(response, "OLD-9")
        self.assertEqual(response.context["owed"], Decimal(300_000))
        bill = Bill.objects.get(store=self.store, number="OLD-9")
        detail = self.client.get(reverse("merchant-bill-detail", args=[bill.pk]))
        self.assertContains(detail, "Saldo awal dari pembukuan lama")
        self.assertNotContains(detail, "Retur ke supplier")
        self.assertContains(self.client.get(reverse("merchant-opening-balances")), url)
