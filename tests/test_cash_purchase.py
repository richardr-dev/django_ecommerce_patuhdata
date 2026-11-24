"""Beli tunai: buy goods and pay on the spot in one step (bill + receive + pay underneath)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting.cash_purchase import CashPurchaseError, buy_for_cash
from apps.accounting.ledger import CASH, INPUT_VAT, INVENTORY, PAYABLE
from apps.accounting.models import ApprovalPolicy, Bill, Supplier
from apps.shops.models import StoreStaff


class CashPurchaseServiceTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="Grosir Sinar")

    def buy(self, **kwargs):
        options = {"supplier": self.supplier, "purchase_date": self.today, "actor": self.owner,
            "lines": [{"sku": "KBL", "qty": Decimal(10), "unit_price": Decimal(70_000)}]}
        options.update(kwargs)
        return buy_for_cash(self.store, **options)

    def test_goods_go_on_the_shelf_and_the_bill_is_paid_from_cash(self):
        bill = self.buy(tax=Decimal(77_000))

        self.assertEqual(bill.status, Bill.STATUS_PAID)
        self.assertEqual(bill.total, Decimal(777_000))
        self.assertEqual(self.on_hand(self.kabel), 110)
        self.kabel.refresh_from_db()
        # Moving average: (100 x 60.000 + 10 x 70.000) / 110.
        self.assertEqual(self.kabel.cost_price, Decimal("60909.09"))
        self.assertEqual(bill.items.get().received_quantity, 10)
        self.assertEqual(balance(self.store, INVENTORY), Decimal(700_000))
        self.assertEqual(balance(self.store, INPUT_VAT), Decimal(77_000))  # PKP: PPN masukan
        self.assertEqual(balance(self.store, PAYABLE), 0)  # bought and paid: nothing owed
        self.assertEqual(balance(self.store, CASH), Decimal(-777_000))
        self.assertEqual(bill.payments.get().method, "cash")

    def test_a_line_that_is_not_in_the_catalog_is_refused_and_nothing_is_booked(self):
        with self.assertRaisesMessage(CashPurchaseError, "tidak ada di katalog"):
            self.buy(lines=[{"sku": "", "description": "Plastik", "qty": Decimal(1), "unit_price": Decimal(5000)},
                {"sku": "KBL", "qty": Decimal(1), "unit_price": Decimal(70_000)}])
        self.assertFalse(Bill.objects.exists())
        self.assertEqual(self.on_hand(self.kabel), 100)

    def test_part_units_are_refused(self):
        with self.assertRaisesMessage(CashPurchaseError, "bilangan bulat"):
            self.buy(lines=[{"sku": "KBL", "qty": Decimal("1.5"), "unit_price": Decimal(70_000)}])


class CashPurchasePageTests(Base):
    def post(self, **extra):
        data = {"idem": "k1", "supplier_id": "new", "supplier_name": "Pasar Baru",
            "purchase_date": self.today.isoformat(), "bank_account": str(self.main.pk),
            "sku[]": ["LMP", ""], "description[]": ["", ""], "qty[]": ["4", "1"],
            "unit_price[]": ["25.000", ""], "tax": "0"}
        data.update(extra)
        return self.client.post(reverse("merchant-cash-purchase"), data)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_page_records_the_purchase_once(self):
        self.assertEqual(self.client.get(reverse("merchant-cash-purchase")).status_code, 200)
        first = self.post()
        bill = Bill.objects.get()
        self.assertRedirects(first, reverse("merchant-bill-detail", args=[bill.pk]), fetch_redirect_response=False)
        self.assertEqual((bill.supplier.name, bill.total, bill.status), ("Pasar Baru", Decimal(100_000), "paid"))
        self.assertEqual(self.on_hand(self.lampu), 104)
        self.post()  # the same form sent twice
        self.assertEqual(Bill.objects.count(), 1)

    def test_above_the_approval_threshold_it_is_refused(self):
        ApprovalPolicy.objects.create(store=self.store, kind="bill_payment", threshold=50_000, enabled=True)
        other = User.objects.create_user("approver")
        StoreStaff.objects.create(store=self.store, user=other, role="finance")
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "batas persetujuan")
        self.assertFalse(Bill.objects.exists())

    def test_a_future_date_is_refused_and_the_rows_are_kept(self):
        response = self.post(purchase_date=(self.today + timedelta(days=2)).isoformat())
        self.assertContains(response, "masa depan")
        self.assertContains(response, 'value="LMP"')
        self.assertFalse(Bill.objects.exists())

    def test_warehouse_staff_cannot_buy_for_cash(self):
        clerk = User.objects.create_user("gudang")
        StoreStaff.objects.create(store=self.store, user=clerk, role="warehouse")
        self.client.force_login(clerk)
        self.assertEqual(self.client.get(reverse("merchant-cash-purchase")).status_code, 404)
