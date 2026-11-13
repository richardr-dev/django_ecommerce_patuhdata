"""Penerimaan barang and GRNI: goods before the bill, then the bill clears the liability."""
from datetime import timedelta
from decimal import Decimal

from test_sales_docs import Base

from apps.accounting import controls, idempotency, receipts
from apps.accounting.ledger import GRNI, INVENTORY, PAYABLE, PURCHASE_VARIANCE, account_balances
from apps.accounting.models import Supplier
from apps.accounting.payables import pay_bill, receive_bill, void_bill


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class GoodsReceiptTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        self.before = balances(self.store)
        self.receipt = receipts.create_receipt(self.store, supplier=self.supplier, received_on=self.today,
            number="SJ-1", lines=[{"sku": "KBL", "qty": Decimal(10), "unit_cost": Decimal(60_000)}],
            actor=self.owner)

    def delta(self, code):
        return balances(self.store).get(code, 0) - self.before.get(code, 0)

    def test_receipt_puts_stock_on_shelf_and_books_grni(self):
        self.assertEqual(self.on_hand(self.kabel), 110)
        self.assertEqual(self.delta(INVENTORY), Decimal(600_000))
        self.assertEqual(self.delta(GRNI), Decimal(600_000))
        self.assertEqual(self.delta(PAYABLE), 0)
        self.assertEqual(controls.grni_tie(self.store, self.today, balances(self.store)).status, controls.OK)

    def test_bill_from_receipt_clears_grni_without_counting_stock_twice(self):
        bill = receipts.bill_receipt(self.receipt, number="INV-9", bill_date=self.today,
            due_date=self.today + timedelta(days=14), actor=self.owner)
        self.assertEqual(self.delta(GRNI), 0)
        self.assertEqual(self.delta(INVENTORY), Decimal(600_000))   # booked once, at receipt
        self.assertEqual(self.delta(PAYABLE), Decimal(600_000))
        self.assertEqual(receive_bill(bill, actor=self.owner), 0)   # nothing left to receive
        self.assertEqual(self.on_hand(self.kabel), 110)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, "billed")
        self.assertEqual(controls.grni_tie(self.store, self.today, balances(self.store)).status, controls.OK)

    def test_price_difference_goes_to_purchase_variance(self):
        line = self.receipt.lines.get()
        receipts.bill_receipt(self.receipt, bill_date=self.today, prices={line.pk: Decimal(62_000)})
        self.assertEqual(self.delta(PURCHASE_VARIANCE), Decimal(20_000))
        self.assertEqual(self.delta(PAYABLE), Decimal(620_000))
        self.assertEqual(self.delta(GRNI), 0)

    def test_cannot_bill_a_receipt_twice_and_void_reopens_it(self):
        bill = receipts.bill_receipt(self.receipt, bill_date=self.today)
        with self.assertRaises(receipts.ReceiptError):
            receipts.bill_receipt(self.receipt, bill_date=self.today)
        void_bill(bill, actor=self.owner)
        self.receipt.refresh_from_db()
        self.assertIsNone(self.receipt.bill_id)
        self.assertEqual(self.delta(GRNI), Decimal(600_000))
        receipts.bill_receipt(self.receipt, bill_date=self.today)  # can be billed again
        self.assertEqual(self.delta(GRNI), 0)

    def test_paying_a_receipt_bill_works(self):
        bill = receipts.bill_receipt(self.receipt, bill_date=self.today)
        pay_bill(bill, amount=Decimal(600_000), paid_on=self.today, actor=self.owner)
        self.assertEqual(self.delta(PAYABLE), 0)

    def test_unknown_sku_and_fractional_units_are_refused(self):
        with self.assertRaises(receipts.ReceiptError):
            receipts.create_receipt(self.store, supplier=self.supplier, received_on=self.today,
                lines=[{"sku": "NOPE", "qty": Decimal(1), "unit_cost": Decimal(1)}])
        with self.assertRaises(receipts.ReceiptError):
            receipts.create_receipt(self.store, supplier=self.supplier, received_on=self.today,
                lines=[{"sku": "KBL", "qty": Decimal("1.5"), "unit_cost": Decimal(1)}])
        self.assertEqual(self.on_hand(self.kabel), 110)

    def test_control_flags_a_manual_grni_journal(self):
        from apps.accounting.ledger import post_journal
        post_journal(self.store, date=self.today, memo="manual", lines=[(GRNI, 0, 5000), (INVENTORY, 5000, 0)])
        self.assertEqual(controls.grni_tie(self.store, self.today, balances(self.store)).status, controls.WARN)


class IdempotencyTests(Base):
    def test_second_use_of_a_key_returns_the_first_result(self):
        self.assertIsNone(idempotency.claim(self.store, "bill", "k1"))
        self.assertEqual(idempotency.claim(self.store, "bill", "k1"), "")       # still running
        idempotency.finish(self.store, "bill", "k1", "abc")
        self.assertEqual(idempotency.claim(self.store, "bill", "k1"), "abc")
        self.assertIsNone(idempotency.claim(self.store, "invoice", "k1"))       # another scope
        self.assertIsNone(idempotency.claim(self.store, "bill", ""))             # no key, no guard

    def test_release_frees_a_failed_key(self):
        idempotency.claim(self.store, "bill", "k2")
        idempotency.release(self.store, "bill", "k2")
        self.assertIsNone(idempotency.claim(self.store, "bill", "k2"))




class GoodsReceiptPageTests(Base):
    def test_pages_receive_and_bill_with_a_double_submit_guard(self):
        from django.urls import reverse
        self.client.force_login(self.owner)
        Supplier.objects.create(store=self.store, name="PT Kabel")
        supplier = Supplier.objects.get()
        self.assertContains(self.client.get(reverse("merchant-goods-receipt-new")), "Terima barang")
        form = {"supplier_id": supplier.pk, "received_on": self.today.isoformat(), "number": "SJ-7",
            "sku[]": ["KBL", ""], "qty[]": ["4", ""], "unit_cost[]": ["60.000", ""], "idem": "abc123"}
        first = self.client.post(reverse("merchant-goods-receipt-new"), form)
        again = self.client.post(reverse("merchant-goods-receipt-new"), form)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(again.status_code, 302)
        from apps.accounting.models import GoodsReceipt
        self.assertEqual(GoodsReceipt.objects.count(), 1)           # the retry made nothing
        self.assertEqual(self.on_hand(self.kabel), 104)             # stock moved once
        receipt = GoodsReceipt.objects.get()
        page = self.client.get(reverse("merchant-goods-receipt", args=[receipt.pk]))
        self.assertContains(page, "Buat tagihan")
        done = self.client.post(reverse("merchant-goods-receipt", args=[receipt.pk]),
            {"bill_date": self.today.isoformat(), "number": "F-1", "idem": "bill1"})
        self.assertEqual(done.status_code, 302)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "billed")
        self.assertContains(self.client.get(reverse("merchant-goods-receipts") + "?show=all"), "Sudah ditagih")

    def test_a_warehouse_user_can_receive_but_not_bill(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        from apps.shops.models import StoreStaff
        keeper = User.objects.create_user("gudang", password="x")
        StoreStaff.objects.create(store=self.store, user=keeper, role="warehouse")
        self.client.force_login(keeper)
        self.assertEqual(self.client.get(reverse("merchant-goods-receipt-new")).status_code, 200)
        supplier = Supplier.objects.create(store=self.store, name="S")
        receipt = receipts.create_receipt(self.store, supplier=supplier, received_on=self.today,
            lines=[{"sku": "KBL", "qty": Decimal(1), "unit_cost": Decimal(10)}])
        self.assertEqual(self.client.post(reverse("merchant-goods-receipt", args=[receipt.pk]),
            {"bill_date": self.today.isoformat()}).status_code, 404)


class FormDoubleSubmitTests(Base):
    def test_invoice_and_bill_forms_do_not_duplicate(self):
        from django.urls import reverse

        from apps.accounting.models import Bill
        from apps.tax.models import Invoice
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-invoice-new")), 'name="idem"')
        self.assertContains(self.client.get(reverse("merchant-bill-new")), 'name="idem"')
        supplier = Supplier.objects.create(store=self.store, name="S")
        bill = {"supplier_id": supplier.pk, "bill_date": self.today.isoformat(), "number": "B-1",
            "description[]": ["Jasa"], "qty[]": ["1"], "unit_price[]": ["1000"], "category": "6200", "idem": "dup1"}
        self.client.post(reverse("merchant-bill-new"), bill)
        self.client.post(reverse("merchant-bill-new"), bill)
        self.assertEqual(Bill.objects.count(), 1)
        before = Invoice.objects.count()
        form = {"buyer_name": "Budi", "phone": "081", "address": "x", "issued_on": self.today.isoformat(),
            "channel": "manual", "payment_status": "credit", "idem": "inv1",
            "product_id[]": [str(self.kabel.pk)], "qty[]": ["1"], "unit_price[]": ["100000"]}
        first = self.client.post(reverse("merchant-invoice-new"), form)
        second = self.client.post(reverse("merchant-invoice-new"), form)
        self.assertLessEqual(Invoice.objects.count() - before, 1)
        if first.status_code == 302:
            self.assertEqual(second.status_code, 302)

    def test_a_crashed_first_run_does_not_block_forever(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.accounting.models import IdempotencyKey
        self.assertIsNone(idempotency.claim(self.store, "bill", "stale"))
        IdempotencyKey.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.assertIsNone(idempotency.claim(self.store, "bill", "stale"))


class BillFormPageTests(Base):
    def test_a_failed_save_keeps_what_was_typed(self):
        from django.urls import reverse
        self.client.force_login(self.owner)
        page = self.client.post(reverse("merchant-bill-new"), {"supplier_id": "", "supplier_name": "", "number": "F-77",
            "bill_date": self.today.isoformat(), "description[]": ["Kabel 2m"], "qty[]": ["3"], "unit_price[]": ["25000"],
            "sku[]": [""], "category": "1400", "notes": "titip"}, follow=True)
        self.assertContains(page, "Pilih atau masukkan nama supplier")
        self.assertContains(page, 'value="F-77"')
        self.assertContains(page, "Kabel 2m")
        self.assertContains(page, "titip")

    def test_the_new_form_has_the_summary_rail(self):
        from django.urls import reverse
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-bill-new"))
        self.assertContains(page, "Total tagihan")
        self.assertContains(page, 'data-rupiah')
        self.assertNotContains(page, "bg-gray-900")
