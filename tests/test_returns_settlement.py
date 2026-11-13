"""A return is settled in one step: credit note and money, stock movements, and the books still
equal the shelf."""
from decimal import Decimal

from django.urls import reverse

from apps.accounting import controls
from apps.accounting.ledger import COGS, INVENTORY, account_balances


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}
from test_sales_docs import Base

from apps.catalog.models import Product, ProductVariant
from apps.orders.models import InventoryMovement, Order, OrderItem, ReturnRequest
from apps.tax import returns
from apps.tax.models import CreditNote


def inventory_check(store, today):
    return next(c for c in controls.run_checks(store, today) if c.key == "inventory")


class RefundReturnTests(Base):
    def returned(self, condition="good"):
        invoice = self.invoice(kabel=2, credit=True)
        line = invoice.order.items.get()
        ret = ReturnRequest.objects.create(order=invoice.order, order_item=line,
            status="inspected", condition=condition)
        return invoice, line, ret

    def test_good_goods_go_back_on_the_shelf_and_reduce_what_is_owed(self):
        invoice, line, ret = self.returned("good")
        self.assertEqual(self.on_hand(self.kabel), 98)
        note = returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today)
        ret.refresh_from_db()
        self.assertEqual((ret.status, ret.credit_note_id), ("refunded", note.pk))
        self.assertEqual(self.on_hand(self.kabel), 99)
        moved = InventoryMovement.objects.get(source_type="credit_note", source_id=str(note.pk))
        self.assertEqual((moved.kind, moved.quantity), ("return", 1))
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_due, Decimal(222_000) - note.total)
        self.assertNotEqual(inventory_check(self.store, self.today).status, controls.FAIL)

    def test_damaged_goods_stay_off_the_shelf_and_the_cost_stays_in_hpp(self):
        invoice, line, ret = self.returned("damaged")
        cogs_before = balances(self.store).get(COGS, 0)
        note = returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 98)
        self.assertFalse(InventoryMovement.objects.filter(source_type="credit_note",
            source_id=str(note.pk)).exists())
        self.assertEqual(balances(self.store).get(COGS, 0), cogs_before)
        self.assertTrue(CreditNote.objects.filter(pk=note.pk).exists())

    def test_cannot_settle_twice_or_before_inspection(self):
        invoice, line, ret = self.returned()
        returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today)
        with self.assertRaises(returns.ReturnError):
            returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today)
        invoice2 = self.invoice(kabel=1)
        early = ReturnRequest.objects.create(order=invoice2.order, status="item_received")
        with self.assertRaises(returns.ReturnError):
            returns.settle_refund(early, quantities={}, issued_on=self.today)

    def test_cannot_return_more_than_was_bought(self):
        invoice, line, ret = self.returned()
        with self.assertRaises(returns.ReturnError):
            returns.settle_refund(ret, quantities={line.pk: 3}, issued_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 98)

    def test_order_without_invoice_gets_one_first(self):
        order = Order.objects.create(store=self.store, customer_name="Tuti", phone="0811",
            shipping_address="-", subtotal=100_000, total=100_000, channel="manual", status="paid",
            idempotency_key="ret-noinv")
        line = OrderItem.objects.create(order=order, product=self.kabel, name="Kabel", sku="KBL",
            unit_price=100_000, quantity=1, line_total=100_000, unit_cost=60_000)
        ret = ReturnRequest.objects.create(order=order, order_item=line, status="inspected",
            condition="good")
        note = returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today)
        self.assertEqual(note.invoice.order_id, order.pk)

    def test_page_flow_inspect_then_settle(self):
        invoice, line, ret = self.returned()
        ret.status, ret.condition = "item_received", ""
        ret.save()
        self.client.force_login(self.owner)
        url = reverse("merchant-return-detail", args=[ret.pk])
        self.client.post(url, {"action": "mark_inspected", "condition": "good"})
        ret.refresh_from_db()
        self.assertEqual((ret.status, ret.condition), ("inspected", "good"))
        self.assertContains(self.client.get(url), "Selesaikan retur")
        self.client.post(url, {"action": "settle_refund", f"qty_{line.pk}": "2",
            "refund_method": "credit", "issued_on": self.today.isoformat()})
        ret.refresh_from_db()
        self.assertEqual(ret.status, "refunded")
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertContains(self.client.get(url), ret.credit_note.number)

    def test_merchant_can_record_a_return_from_the_order(self):
        invoice = self.invoice()
        line = invoice.order.items.get()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("merchant-return-create", args=[invoice.order.id]),
            {"order_item": line.pk, "request_type": "return_refund", "reason": "defective"})
        ret = ReturnRequest.objects.get(order=invoice.order)
        self.assertRedirects(response, reverse("merchant-return-detail", args=[ret.pk]))
        self.assertEqual((ret.status, ret.reason), ("requested", "defective"))


class ExchangeTests(Base):
    def setUp(self):
        super().setUp()
        self.shirt = Product.objects.create(store=self.store, sku="KMJ", name="Kemeja", slug="kemeja",
            price=150_000, cost_price=80_000)
        self.m = ProductVariant.objects.create(product=self.shirt, name="M", sku="KMJ-M", stock=5)
        self.l = ProductVariant.objects.create(product=self.shirt, name="L", sku="KMJ-L", stock=3)
        order = Order.objects.create(store=self.store, customer_name="Ani", phone="0812",
            shipping_address="-", subtotal=150_000, total=150_000, channel="manual",
            status="delivered", idempotency_key="ex-1")
        self.line = OrderItem.objects.create(order=order, product=self.shirt, variant=self.m,
            name="Kemeja", variant_name="M", sku="KMJ-M", unit_price=150_000, quantity=1,
            line_total=150_000, unit_cost=80_000)
        self.ret = ReturnRequest.objects.create(order=order, order_item=self.line,
            request_type="exchange", status="inspected", condition="good", original_variant=self.m)

    def stock(self, variant):
        variant.refresh_from_db()
        return variant.stock

    def test_good_original_returns_and_replacement_leaves(self):
        returns.settle_exchange(self.ret, replacement_variant=self.l)
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (6, 2))
        self.ret.refresh_from_db()
        self.assertEqual(self.ret.status, "replacement_sent")
        self.assertEqual(InventoryMovement.objects.filter(source_type="return",
            source_id=str(self.ret.pk)).count(), 2)

    def test_damaged_original_is_booked_as_a_loss(self):
        self.ret.condition = "damaged"
        self.ret.save()
        before = balances(self.store)
        returns.settle_exchange(self.ret, replacement_variant=self.l)
        self.assertEqual((self.stock(self.m), self.stock(self.l)), (5, 2))
        after = balances(self.store)
        self.assertEqual(after.get(COGS, 0) - before.get(COGS, 0), Decimal(80_000))
        self.assertEqual(after.get(INVENTORY, 0) - before.get(INVENTORY, 0), Decimal(-80_000))

    def test_replacement_must_be_in_stock_and_different(self):
        self.l.stock = 0
        self.l.save()
        with self.assertRaises(returns.ReturnError):
            returns.settle_exchange(self.ret, replacement_variant=self.l)
        with self.assertRaises(returns.ReturnError):
            returns.settle_exchange(self.ret, replacement_variant=self.m)
        self.assertEqual(self.stock(self.m), 5)


class MarketplaceReturnTests(Base):
    def make(self, channel):
        order = Order.objects.create(store=self.store, customer_name="Rina", phone="0813",
            shipping_address="-", subtotal=200_000, total=200_000, channel=channel, status="completed",
            idempotency_key=f"mk-{channel}")
        line = OrderItem.objects.create(order=order, product=self.kabel, name="Kabel", sku="KBL",
            unit_price=100_000, quantity=2, line_total=200_000, unit_cost=60_000)
        ret = ReturnRequest.objects.create(order=order, order_item=line, status="inspected",
            condition="good")
        return order, line, ret

    def test_marketplace_return_is_taken_off_the_marketplace_receivable(self):
        from apps.accounting.ledger import post_everything
        from apps.orders.models import PaymentTransaction
        order, line, ret = self.make("shopee")
        PaymentTransaction.objects.create(order=order, provider="shopee", external_id="SHP-1",
            kind="payment", status="succeeded", amount=200_000)
        returns.return_invoice(ret)  # issue the invoice first so only the return is measured
        post_everything()
        before = balances(self.store)
        note = returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today,
            excess_action="platform")
        after = balances(self.store)
        self.assertEqual(note.excess_action, "platform")
        self.assertEqual(after.get("1300", 0) - before.get("1300", 0), -note.total)
        self.assertEqual(after.get("1100", 0), before.get("1100", 0))  # no money left the bank
        self.assertEqual(self.on_hand(self.kabel), 101)

    def test_only_marketplace_orders_can_use_it(self):
        order, line, ret = self.make("manual")
        with self.assertRaises(returns.ReturnError):
            returns.settle_refund(ret, quantities={line.pk: 1}, issued_on=self.today,
                excess_action="platform")

    def test_return_page_offers_it_for_shopee_only(self):
        order, line, ret = self.make("shopee")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-return-detail", args=[ret.pk]))
        self.assertContains(page, "Dipotong dari pencairan Shopee")
        order2, line2, ret2 = self.make("manual")
        self.assertNotContains(self.client.get(reverse("merchant-return-detail", args=[ret2.pk])),
            "Dipotong dari pencairan")
