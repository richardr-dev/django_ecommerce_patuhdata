"""Stock moves by documents, leaves movements, and consignment stays the store's until sold."""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounting.ledger import (
    CASH,
    COGS,
    CONSIGNED,
    INVENTORY,
    RECEIVABLE,
    SALES,
    account_balances,
)
from apps.accounting.models import Bill, Supplier
from apps.accounting.payables import PayableError, create_bill, receive_bill
from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import InventoryMovement, Order, OrderItem
from apps.orders.stock import (
    StockError,
    count_stock,
    record_opening_balances,
    stock_audit,
    transfer_stock,
)
from apps.settlements.consignment import (
    ConsignmentError,
    return_from_partner,
    send_to_partner,
    stock_at_partners,
)
from apps.settlements.models import ConsignmentPartner, ConsignmentStock, Settlement, SettlementLine
from apps.settlements.services import record_payment, transition
from apps.shops.models import Store
from apps.tax.models import Invoice

TODAY = date(2026, 11, 12)


def _balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("stock-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-stok")
        self.gudang = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=30000, cost_price=Decimal(10000))
        self.level = InventoryLevel.objects.create(warehouse=self.gudang, product=self.kopi,
            on_hand=10)
        self.teh = Product.objects.create(store=self.store, name="Teh", slug="teh", sku="TEH",
            price=20000)
        self.teh_m = ProductVariant.objects.create(product=self.teh, name="M", sku="TEH-M",
            stock=5, cost_price=Decimal(8000))

    def on_hand(self):
        self.level.refresh_from_db()
        return self.level.on_hand


class GoodsReceiptTests(Base):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(store=self.store, name="PT Kopi")

    def _bill(self, sku="KOPI", qty=10, price=14000, tax=0):
        return create_bill(self.store, supplier=self.supplier, bill_date=TODAY, tax=Decimal(tax),
            lines=[{"description": "Biji kopi", "qty": Decimal(qty),
                "unit_price": Decimal(price), "sku": sku}])

    def test_receiving_adds_stock_and_averages_the_cost(self):
        bill = self._bill()

        self.assertEqual(receive_bill(bill), 10)

        self.kopi.refresh_from_db()
        self.assertEqual(self.on_hand(), 20)
        # 10 on the shelf at 10.000 and 10 arriving at 14.000 average to 12.000.
        self.assertEqual(self.kopi.cost_price, Decimal(12000))
        movement = InventoryMovement.objects.get(kind="receipt")
        self.assertEqual((movement.source_type, movement.source_id), ("bill", str(bill.pk)))
        self.assertEqual(receive_bill(bill), 0)
        self.assertEqual(self.on_hand(), 20)

    def test_a_non_pkp_buyer_carries_the_ppn_in_the_cost(self):
        bill = self._bill(qty=10, price=10000, tax=11000)

        receive_bill(bill)

        self.kopi.refresh_from_db()
        # 10 × 10.000 plus 11.000 PPN is 11.100 per unit, averaged with the 10 held at 10.000.
        self.assertEqual(self.kopi.cost_price, Decimal("10550.00"))

    def test_variant_goods_and_unknown_sku(self):
        receive_bill(self._bill(sku="TEH-M", qty=5, price=9000))
        self.teh_m.refresh_from_db()
        self.assertEqual(self.teh_m.stock, 10)
        self.assertEqual(self.teh_m.cost_price, Decimal("8500.00"))

        with self.assertRaises(PayableError):
            self._bill(sku="TIDAK-ADA")

    def test_voided_bill_cannot_be_received(self):
        bill = self._bill()
        bill.status = Bill.STATUS_VOID
        bill.save()
        with self.assertRaises(PayableError):
            receive_bill(bill)


class CountAndAuditTests(Base):
    def test_opname_records_the_difference(self):
        count_stock(store=self.store, product=self.kopi, counted=7, reason="Hilang di gudang")

        self.assertEqual(self.on_hand(), 7)
        movement = InventoryMovement.objects.get(kind="opname")
        self.assertEqual((movement.previous_on_hand, movement.new_on_hand), (10, 7))
        with self.assertRaises(StockError):
            count_stock(store=self.store, product=self.kopi, counted=-1)

    def test_transfer_between_warehouses(self):
        toko = Warehouse.objects.create(store=self.store, name="Outlet", code="O1")

        transfer_stock(store=self.store, product=self.kopi, quantity=4, source=self.gudang,
            target=toko)

        self.assertEqual(self.on_hand(), 6)
        self.assertEqual(InventoryLevel.objects.get(warehouse=toko, product=self.kopi).on_hand, 4)
        with self.assertRaises(StockError):
            transfer_stock(store=self.store, product=self.kopi, quantity=7, source=self.gudang,
                target=toko)

    def test_audit_finds_untracked_stock_and_opening_balances_fix_it(self):
        self.assertEqual(len(stock_audit(self.store)), 2)  # created without movements

        record_opening_balances(self.store)

        self.assertEqual(stock_audit(self.store), [])
        count_stock(store=self.store, product=self.kopi, counted=3)
        self.assertEqual(stock_audit(self.store), [])


class ConsignmentTests(Base):
    def setUp(self):
        super().setUp()
        self.partner = ConsignmentPartner.objects.create(store=self.store, name="AB Shop")

    def test_sending_is_not_a_sale_and_stock_stays_in_inventory_value(self):
        send_to_partner(self.partner, product=self.kopi, quantity=4, date=TODAY)

        self.assertEqual(self.on_hand(), 6)
        self.assertEqual(ConsignmentStock.objects.get().quantity, 4)
        self.assertEqual(_balance(self.store, CONSIGNED), Decimal(40000))
        self.assertEqual(_balance(self.store, INVENTORY), Decimal(-40000))
        self.assertEqual(_balance(self.store, SALES), 0)
        self.assertEqual(stock_at_partners(self.store)[0]["value"], Decimal(40000))

    def test_variant_goes_to_the_mitra_too(self):
        send_to_partner(self.partner, product=self.teh, variant=self.teh_m, quantity=2,
            date=TODAY)

        self.teh_m.refresh_from_db()
        self.assertEqual(self.teh_m.stock, 3)
        self.assertEqual(ConsignmentStock.objects.get().variant, self.teh_m)

    def test_cannot_send_more_than_the_shelf_holds_or_return_more_than_the_mitra_has(self):
        with self.assertRaises(ConsignmentError):
            send_to_partner(self.partner, product=self.kopi, quantity=11)
        send_to_partner(self.partner, product=self.kopi, quantity=3, date=TODAY)
        with self.assertRaises(ConsignmentError):
            return_from_partner(self.partner, product=self.kopi, quantity=4)

    def test_return_brings_stock_and_value_back(self):
        send_to_partner(self.partner, product=self.kopi, quantity=4, date=TODAY)

        return_from_partner(self.partner, product=self.kopi, quantity=1, date=TODAY)

        self.assertEqual(self.on_hand(), 7)
        self.assertEqual(_balance(self.store, CONSIGNED), Decimal(30000))

    def test_invoicing_a_settlement_books_the_sale_and_empties_the_mitra_stock(self):
        send_to_partner(self.partner, product=self.kopi, quantity=4, date=TODAY)
        order = Order.objects.create(store=self.store, customer_name="AB Shop", phone="-",
            shipping_address="-", subtotal=Decimal(60000), total=Decimal(60000),
            channel="abshop", order_type="reseller_sell_through", idempotency_key="mitra-1",
            consignment_partner=self.partner)
        OrderItem.objects.create(order=order, product=self.kopi, name="Kopi", sku="KOPI",
            unit_price=Decimal(30000), quantity=2, line_total=Decimal(60000))
        settlement = Settlement.objects.create(store=self.store, partner=self.partner,
            period_start=TODAY, period_end=TODAY, status=Settlement.STATUS_CONFIRMED)
        SettlementLine.objects.create(settlement=settlement, order=order, description="x",
            sale_date=TODAY, line_total=Decimal(50000))
        settlement.recalculate_totals()
        settlement.save()

        transition(settlement, Settlement.STATUS_INVOICED)

        self.assertEqual(ConsignmentStock.objects.get().quantity, 2)
        self.assertEqual(_balance(self.store, RECEIVABLE), Decimal(50000))
        self.assertEqual(_balance(self.store, SALES), Decimal(50000))
        self.assertEqual(_balance(self.store, COGS), Decimal(20000))
        self.assertEqual(_balance(self.store, CONSIGNED), Decimal(20000))

        settlement.refresh_from_db()
        record_payment(settlement, Decimal(50000), TODAY)
        self.assertEqual(_balance(self.store, RECEIVABLE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(50000))


class InvoiceVoidRestocksTests(Base):
    def test_voiding_a_manual_invoice_returns_its_stock(self):
        from apps.orders.inventory import deduct_invoice_stock
        from apps.tax.posting import void_invoice
        from apps.tax.services import issue_invoice_for_order

        order = Order.objects.create(store=self.store, customer_name="Budi", phone="-",
            shipping_address="-", subtotal=Decimal(60000), total=Decimal(60000), status="paid",
            channel="manual", idempotency_key="void-1")
        OrderItem.objects.create(order=order, product=self.kopi, name="Kopi", sku="KOPI",
            unit_price=Decimal(30000), quantity=2, line_total=Decimal(60000))
        deduct_invoice_stock(order)
        invoice, _ = issue_invoice_for_order(order, issued_on=TODAY)
        self.assertEqual(self.on_hand(), 8)

        void_invoice(invoice)

        self.assertEqual(self.on_hand(), 10)
        self.assertEqual(Invoice.objects.get().status, Invoice.STATUS_VOID)
