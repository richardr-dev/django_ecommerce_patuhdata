"""Pesanan pembelian: order → partial receipts → bill, matched three ways."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import controls, purchasing
from apps.accounting.ledger import GRNI, INVENTORY, PAYABLE, account_balances
from apps.accounting.models import PurchaseOrder, Supplier
from apps.accounting.receipts import bill_receipt
from apps.shops.models import StoreStaff


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class PurchaseOrderTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        self.po = purchasing.create_po(self.store, supplier=self.supplier, order_date=self.today,
            expected_on=self.today + timedelta(days=5), actor=self.owner,
            lines=[{"sku": "KBL", "qty": Decimal(10), "unit_cost": Decimal(60_000)},
                   {"sku": "LMP", "qty": Decimal(4), "unit_cost": Decimal(30_000)}])
        self.before = balances(self.store)

    def line(self, sku):
        return next(line for line in self.po.lines.all() if line.product.sku == sku)

    def test_an_order_books_nothing_and_gets_a_number(self):
        self.assertEqual(self.po.number, f"PO/{self.today:%Y}/0001")
        self.assertEqual(self.po.value, Decimal(720_000))
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertEqual(purchasing.waiting_value(self.store), Decimal(720_000))
        second = purchasing.create_po(self.store, supplier=self.supplier, order_date=self.today,
            lines=[{"sku": "KBL", "qty": Decimal(1), "unit_cost": Decimal(1)}])
        self.assertTrue(second.number.endswith("0002"))

    def test_partial_receipts_until_the_order_is_complete(self):
        purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 6}, received_on=self.today, actor=self.owner)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.PARTIAL)
        self.assertEqual(self.on_hand(self.kabel), 106)
        self.assertEqual(balances(self.store)[GRNI] - self.before[GRNI], Decimal(360_000))
        self.assertEqual(purchasing.waiting_value(self.store), Decimal(360_000))
        purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 4, self.line("LMP").pk: 4},
            received_on=self.today, actor=self.owner)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.RECEIVED)
        self.assertEqual(purchasing.waiting_value(self.store), 0)
        self.assertEqual(balances(self.store)[GRNI] - self.before[GRNI], Decimal(720_000))

    def test_cannot_receive_more_than_is_open_or_on_a_finished_order(self):
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 11}, received_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 100)                         # nothing moved
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive_po(self.po, quantities={}, received_on=self.today)
        purchasing.close_po(self.po)
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 1}, received_on=self.today)

    def test_closing_keeps_what_arrived_and_stops_waiting_for_the_rest(self):
        purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 3}, received_on=self.today)
        purchasing.close_po(self.po)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.CLOSED)
        self.assertEqual(purchasing.waiting_value(self.store), 0)
        fresh = purchasing.create_po(self.store, supplier=self.supplier, order_date=self.today,
            lines=[{"sku": "KBL", "qty": Decimal(1), "unit_cost": Decimal(1)}])
        self.assertEqual(purchasing.close_po(fresh).status, PurchaseOrder.CANCELLED)

    def test_the_bill_is_matched_three_ways_and_price_differences_are_booked(self):
        receipt = purchasing.receive_po(self.po, quantities={self.line("KBL").pk: 10}, received_on=self.today)
        self.assertEqual(self.line("KBL").billed_quantity, 0)
        price = receipt.lines.get()
        bill = bill_receipt(receipt, bill_date=self.today, prices={price.pk: Decimal(62_000)})
        self.assertEqual(self.line("KBL").billed_quantity, 10)
        self.assertEqual(bill.total, Decimal(620_000))
        after = balances(self.store)
        self.assertEqual(after[GRNI] - self.before[GRNI], 0)
        self.assertEqual(after[PAYABLE] - self.before[PAYABLE], Decimal(620_000))
        self.assertEqual(after["5150"], Decimal(20_000))                        # price above the order
        self.assertEqual(after[INVENTORY] - self.before[INVENTORY], Decimal(600_000))

    def test_a_receipt_must_come_from_the_orders_supplier(self):
        other = Supplier.objects.create(store=self.store, name="PT Lain")
        from apps.accounting.receipts import ReceiptError, create_receipt
        with self.assertRaises(ReceiptError):
            create_receipt(self.store, supplier=other, received_on=self.today, purchase_order=self.po,
                lines=[{"sku": "KBL", "qty": Decimal(1), "unit_cost": Decimal(1), "po_line": self.line("KBL")}])

    def test_overdue_orders_show_in_the_checks(self):
        self.assertEqual(controls.purchase_orders_late(self.store, self.today, {}).status, controls.OK)
        late = self.today + timedelta(days=9)
        check = controls.purchase_orders_late(self.store, late, {})
        self.assertEqual(check.status, controls.INFO)
        self.assertIn(self.po.number, check.summary)


class PurchaseOrderPageTests(Base):
    def test_pages_order_receive_and_roles(self):
        self.client.force_login(self.owner)
        supplier = Supplier.objects.create(store=self.store, name="PT Kabel")
        form = {"supplier_id": supplier.pk, "order_date": self.today.isoformat(), "sku[]": ["KBL", ""],
            "qty[]": ["5", ""], "unit_cost[]": ["60.000", ""], "idem": "po1"}
        self.client.post(reverse("merchant-purchase-order-new"), form)
        self.client.post(reverse("merchant-purchase-order-new"), form)
        self.assertEqual(PurchaseOrder.objects.count(), 1)                      # double submit made one
        po = PurchaseOrder.objects.get()
        page = self.client.get(reverse("merchant-purchase-order", args=[po.pk]))
        self.assertContains(page, "Dipesan, diterima, ditagih")
        line = po.lines.get()
        done = self.client.post(reverse("merchant-purchase-order", args=[po.pk]),
            {"action": "receive", "received_on": self.today.isoformat(), f"qty_{line.pk}": "3", "idem": "r1"})
        self.assertEqual(done.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.PARTIAL)
        self.assertEqual(self.on_hand(self.kabel), 103)
        self.assertContains(self.client.get(reverse("merchant-purchase-orders") + "?show=all"), po.number)
        keeper = User.objects.create_user("gudang", password="x")
        StoreStaff.objects.create(store=self.store, user=keeper, role="warehouse")
        self.client.force_login(keeper)                                          # warehouse: sees, receives, cannot order
        self.assertEqual(self.client.get(reverse("merchant-purchase-order-new")).status_code, 404)
        self.assertEqual(self.client.post(reverse("merchant-purchase-order", args=[po.pk]), {"action": "close"}).status_code, 404)
        self.client.post(reverse("merchant-purchase-order", args=[po.pk]),
            {"action": "receive", "received_on": self.today.isoformat(), f"qty_{line.pk}": "2", "idem": "r2"})
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.RECEIVED)
