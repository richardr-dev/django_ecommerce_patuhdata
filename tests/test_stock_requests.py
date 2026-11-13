"""Permintaan stok: outlet asks the warehouse; goods in transit; loss in transit is booked."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import inventory
from apps.accounting.ledger import INVENTORY, STOCK_DIFFERENCE, account_balances
from apps.catalog.models import InventoryLevel, Warehouse
from apps.orders import stock_requests
from apps.orders.models import InventoryMovement, StockRequest
from apps.orders.stock import record_opening_balances, stock_audit
from apps.shops.models import StoreStaff


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class StockRequestTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.main = Warehouse.objects.get(store=self.store)
        self.outlet = Warehouse.objects.create(store=self.store, code="OUT1", name="Outlet Kemang", is_outlet=True)
        self.gap = inventory.reconciliation(self.store)["difference"]
        self.before = balances(self.store)
        self.clerk = User.objects.create_user("kasir", password="x")
        StoreStaff.objects.create(store=self.store, user=self.clerk, role="warehouse")
        self.manager = User.objects.create_user("mgr", password="x")
        StoreStaff.objects.create(store=self.store, user=self.manager, role="manager")
        self.req = stock_requests.create_request(self.store, from_warehouse=self.main, to_warehouse=self.outlet,
            lines=[{"sku": "KBL", "qty": Decimal(30)}], actor=self.clerk)

    def level(self, warehouse, product=None):
        return InventoryLevel.objects.filter(warehouse=warehouse, product=product or self.kabel).first()

    def line(self):
        return self.req.lines.get()

    def test_the_full_flow_moves_stock_without_touching_the_books(self):
        stock_requests.approve(self.req, actor=self.manager)
        self.assertEqual(self.level(self.main).on_hand, 100)                            # approval moves nothing
        stock_requests.send(self.req, quantities={self.line().pk: 30}, actor=self.clerk)
        self.assertEqual(self.level(self.main).on_hand, 70)
        self.assertIsNone(self.level(self.outlet))                                      # in transit: on no shelf yet
        self.assertEqual(stock_requests.in_transit_value(self.store), Decimal(30 * 60_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)  # transit is counted
        stock_requests.receive(self.req, quantities={self.line().pk: 30}, actor=self.clerk)
        self.assertEqual(self.level(self.outlet).on_hand, 30)
        self.assertEqual(stock_requests.in_transit_value(self.store), 0)
        self.assertEqual(balances(self.store), self.before)                             # no journal at all
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)
        self.assertEqual(list(InventoryMovement.objects.filter(source_type="stock_request").order_by("id")
            .values_list("kind", "quantity")), [("transfer_out", 30), ("transfer_in", 30)])
        self.assertEqual(stock_audit(self.store), [])
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, StockRequest.RECEIVED)

    def test_a_shortfall_on_receipt_is_a_loss_in_transit_at_cost(self):
        stock_requests.approve(self.req, actor=self.manager)
        stock_requests.send(self.req, quantities={self.line().pk: 30}, actor=self.clerk)
        stock_requests.receive(self.req, quantities={self.line().pk: 27}, actor=self.clerk)
        self.assertEqual(self.level(self.outlet).on_hand, 27)
        after = balances(self.store)
        self.assertEqual(after[STOCK_DIFFERENCE] - self.before.get(STOCK_DIFFERENCE, 0), Decimal(3 * 60_000))
        self.assertEqual(after[INVENTORY] - self.before[INVENTORY], Decimal(-3 * 60_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], self.gap)

    def test_sending_is_limited_to_what_was_asked_and_the_free_stock(self):
        stock_requests.approve(self.req, actor=self.manager)
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.send(self.req, quantities={self.line().pk: 31})
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.send(self.req, quantities={})
        self.invoice(kabel=80, lampu=0)                                                 # leaves 20 free
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.send(self.req, quantities={self.line().pk: 30})
        self.assertEqual(self.level(self.main).on_hand, 20)                             # nothing left in transit

    def test_the_requester_cannot_approve_and_rules_of_state(self):
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.send(self.req, quantities={self.line().pk: 1})               # not approved yet
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.approve(self.req, actor=self.clerk)                          # a warehouse role cannot approve
        owner_req = stock_requests.create_request(self.store, from_warehouse=self.main, to_warehouse=self.outlet,
            lines=[{"sku": "LMP", "qty": Decimal(5)}], actor=self.manager)
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.approve(owner_req, actor=self.manager)                       # made it, others could approve
        self.assertEqual(stock_requests.approve(owner_req, actor=self.owner).status, StockRequest.APPROVED)
        rejected = stock_requests.approve(self.req, actor=self.manager, reject=True)
        self.assertEqual(rejected.status, StockRequest.REJECTED)
        with self.assertRaises(stock_requests.RequestError):
            stock_requests.approve(self.req, actor=self.manager)                        # decided once

    def test_bad_requests_are_refused(self):
        for lines, warehouse in (([{"sku": "NOPE", "qty": Decimal(1)}], self.outlet), ([{"sku": "KBL", "qty": Decimal("1.5")}], self.outlet),
                ([], self.outlet), ([{"sku": "KBL", "qty": Decimal(1)}], self.main)):
            with self.assertRaises(stock_requests.RequestError):
                stock_requests.create_request(self.store, from_warehouse=self.main, to_warehouse=warehouse, lines=lines)

    def test_pages(self):
        self.client.force_login(self.clerk)
        self.assertContains(self.client.get(reverse("merchant-stock-requests")), self.req.number)
        self.assertContains(self.client.get(reverse("merchant-stock-request", args=[self.req.pk])), "Diminta")
        form = {"from_warehouse": self.main.pk, "to_warehouse": self.outlet.pk, "sku[]": ["LMP"], "qty[]": ["4"], "idem": "sr1"}
        self.client.post(reverse("merchant-stock-request-new"), form)
        self.client.post(reverse("merchant-stock-request-new"), form)
        self.assertEqual(StockRequest.objects.count(), 2)                                # the double submit made one
        self.client.post(reverse("merchant-stock-request", args=[self.req.pk]), {"action": "approve"})
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, StockRequest.REQUESTED)                        # a clerk cannot approve
        self.client.force_login(self.manager)
        self.client.post(reverse("merchant-stock-request", args=[self.req.pk]), {"action": "approve"})
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, StockRequest.APPROVED)
