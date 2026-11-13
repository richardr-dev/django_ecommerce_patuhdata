"""Nota debit (retur ke supplier) and paying several bills at once."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import controls, supplier_notes
from apps.accounting.ledger import INPUT_VAT, INVENTORY, PAYABLE, account_balances
from apps.accounting.models import Bill, Supplier
from apps.accounting.payables import create_bill, receive_bill
from apps.orders.models import InventoryMovement
from apps.shops.models import StoreStaff
from apps.tax.models import TaxProfile


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class DebitNoteTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        self.bill = create_bill(self.store, supplier=self.supplier, bill_date=self.today, actor=self.owner,
            due_date=self.today + timedelta(days=14), tax=Decimal(110_000),
            lines=[{"description": "Kabel", "qty": Decimal(10), "unit_price": Decimal(100_000), "sku": "KBL"}])
        receive_bill(self.bill, actor=self.owner)
        self.item = self.bill.items.get()
        self.before = balances(self.store)

    def delta(self, code):
        return balances(self.store).get(code, 0) - self.before.get(code, 0)

    def test_returning_goods_lowers_the_debt_stock_and_inventory(self):
        note = supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 4}, reason="rusak",
            note_date=self.today, actor=self.owner)
        self.assertEqual((note.subtotal, note.tax, note.total), (Decimal(400_000), Decimal(44_000), Decimal(444_000)))
        self.assertEqual(self.on_hand(self.kabel), 106)                              # 100 + 10 received - 4 returned
        self.assertEqual(self.delta(PAYABLE), Decimal(-444_000))                      # the debt goes down
        self.assertEqual(self.delta(INVENTORY), Decimal(-400_000))
        self.assertEqual(self.delta(INPUT_VAT), Decimal(-44_000))                     # PKP: PPN masukan reversed
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.amount_due, Decimal(1_110_000 - 444_000))
        self.assertEqual(InventoryMovement.objects.filter(kind="purchase_return").get().quantity, 4)

    def test_cannot_return_more_than_received_or_already_returned(self):
        supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 8}, reason="x", note_date=self.today)
        with self.assertRaises(supplier_notes.NoteError):
            supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 3}, reason="x", note_date=self.today)
        with self.assertRaises(supplier_notes.NoteError):
            supplier_notes.create_debit_note(self.bill, quantities={}, reason="x", note_date=self.today)
        with self.assertRaises(supplier_notes.NoteError):
            supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 1}, reason="", note_date=self.today)

    def test_goods_not_yet_received_cannot_go_back(self):
        later = create_bill(self.store, supplier=self.supplier, bill_date=self.today,
            lines=[{"description": "Kabel", "qty": Decimal(5), "unit_price": Decimal(100_000), "sku": "KBL"}])
        with self.assertRaises(supplier_notes.NoteError):
            supplier_notes.create_debit_note(later, quantities={later.items.get().pk: 1}, reason="x", note_date=self.today)

    def test_a_note_cannot_exceed_what_is_unpaid(self):
        from apps.accounting.payables import pay_bill
        pay_bill(self.bill, amount=Decimal(1_000_000), paid_on=self.today, actor=self.owner)
        with self.assertRaises(supplier_notes.NoteError) as raised:
            supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 10}, reason="x", note_date=self.today)
        self.assertIn("penerimaan kas", str(raised.exception))
        self.assertEqual(self.on_hand(self.kabel), 110)                               # nothing was taken

    def test_a_returned_in_full_bill_is_settled_and_a_non_pkp_loses_no_vat_entry(self):
        TaxProfile.objects.filter(store=self.store).update(is_pkp=False)
        plain = create_bill(self.store, supplier=self.supplier, bill_date=self.today, tax=Decimal(110_000),
            lines=[{"description": "Kabel", "qty": Decimal(2), "unit_price": Decimal(100_000), "sku": "KBL"}])
        receive_bill(plain, actor=self.owner)
        vat_before = balances(self.store).get(INPUT_VAT, 0)
        supplier_notes.create_debit_note(plain, quantities={plain.items.get().pk: 2}, reason="salah kirim", note_date=self.today)
        plain.refresh_from_db()
        self.assertEqual(plain.amount_due, 0)                                          # 200.000 + 110.000 PPN, all returned
        self.assertEqual(plain.status, Bill.STATUS_PAID)
        self.assertEqual(balances(self.store).get(INPUT_VAT, 0), vat_before)

    def test_ledger_and_aging_still_agree_after_notes(self):
        supplier_notes.create_debit_note(self.bill, quantities={self.item.pk: 4}, reason="rusak", note_date=self.today)
        self.assertEqual(controls.payable(self.store, self.today, balances(self.store)).status, controls.OK)

    def test_pay_several_bills_at_once(self):
        second = create_bill(self.store, supplier=self.supplier, bill_date=self.today,
            lines=[{"description": "Jasa", "qty": 1, "unit_price": Decimal(300_000)}], category="6200")
        payments = supplier_notes.pay_bills([(self.bill, Decimal(500_000)), (second, Decimal(300_000))],
            paid_on=self.today, actor=self.owner, reference="TRF-1")
        self.assertEqual(len(payments), 2)
        second.refresh_from_db()
        self.assertEqual(second.status, Bill.STATUS_PAID)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, Bill.STATUS_PARTIAL)
        other = Supplier.objects.create(store=self.store, name="Lain")
        foreign = create_bill(self.store, supplier=other, bill_date=self.today,
            lines=[{"description": "X", "qty": 1, "unit_price": Decimal(1000)}], category="6200")
        with self.assertRaises(supplier_notes.NoteError):                              # one supplier per payment
            supplier_notes.pay_bills([(self.bill, Decimal(1)), (foreign, Decimal(1))], paid_on=self.today)
        with self.assertRaises(supplier_notes.NoteError):                              # all or nothing
            supplier_notes.pay_bills([(self.bill, Decimal(1000)), (second, Decimal(1))], paid_on=self.today)


class SupplierPageTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        self.maker = User.objects.create_user("maker", password="x")
        StoreStaff.objects.create(store=self.store, user=self.maker, role="finance")
        self.bill = create_bill(self.store, supplier=self.supplier, bill_date=self.today, actor=self.maker,
            lines=[{"description": "Kabel", "qty": Decimal(5), "unit_price": Decimal(100_000), "sku": "KBL"}])
        receive_bill(self.bill, actor=self.owner)

    def test_debit_note_pages_and_double_submit(self):
        self.client.force_login(self.owner)
        item = self.bill.items.get()
        page = self.client.get(reverse("merchant-debit-note-new") + f"?bill={self.bill.pk}")
        self.assertContains(page, "Retur ke supplier")
        form = {"bill": self.bill.pk, f"qty_{item.pk}": "2", "reason": "rusak", "note_date": self.today.isoformat(), "idem": "dn1"}
        self.client.post(reverse("merchant-debit-note-new"), form)
        self.client.post(reverse("merchant-debit-note-new"), form)
        from apps.accounting.models import DebitNote
        self.assertEqual(DebitNote.objects.count(), 1)
        note = DebitNote.objects.get()
        self.assertContains(self.client.get(reverse("merchant-debit-note", args=[note.pk])), note.number)
        self.assertContains(self.client.get(reverse("merchant-debit-notes")), note.number)
        self.assertContains(self.client.get(reverse("merchant-bill-detail", args=[self.bill.pk])), "Nota debit")

    def test_the_multi_payment_page_pays_and_respects_the_maker_rule(self):
        self.client.force_login(self.maker)                                            # made the bill: may not pay it
        self.assertContains(self.client.get(reverse("merchant-supplier-pay", args=[self.supplier.pk])), "Bayar beberapa")
        self.client.post(reverse("merchant-supplier-pay", args=[self.supplier.pk]),
            {f"amount_{self.bill.pk}": "500000", "paid_on": self.today.isoformat(), "method": "transfer", "idem": "p1"})
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, "open")
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-supplier-pay", args=[self.supplier.pk]),
            {f"amount_{self.bill.pk}": "500000", "paid_on": self.today.isoformat(), "method": "transfer", "idem": "p2"})
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, "paid")
