"""Payout reconciliation and cost of goods: gateway and marketplace money is settled order by
order into the bank, fees become an expense, and each sale books its cost."""
import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from apps.accounting.ledger import (
    CASH,
    COGS,
    FEES,
    GATEWAY,
    INVENTORY,
    MARKETPLACE,
    account_balances,
    post_pending_entries,
)
from apps.accounting.models import Payout
from apps.accounting.payouts import (
    PayoutImportError,
    import_payout,
    outstanding_by_provider,
    parse_payouts_csv,
)
from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.models import SalesChannel
from apps.channels.services import import_order
from apps.integrations.reconciliation import confirm_payment
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store


def _balances(store):
    return {account.code: account.balance for account in account_balances(store)}


class PayoutTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000, cost_price=30000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=50)

    def _gateway_order(self, reference, quantity=2, provider="xendit"):
        total = Decimal(50000 * quantity)
        order = Order.objects.create(store=self.store, customer_name="Ayu", email="a@b.id",
            phone="0812", shipping_address="Bandung", subtotal=total, total=total,
            payment_provider=provider, payment_reference=reference, paid_to_store_account=True,
            idempotency_key=uuid.uuid4().hex)
        OrderItem.objects.create(order=order, product=self.kopi, name="Kopi", sku="KOPI",
            unit_price=50000, quantity=quantity, line_total=total)
        confirm_payment(order, provider=provider, external_id=f"x-{reference}", amount=total,
            currency="IDR", payload={}, note="Lunas")
        return order

    def _payout(self, rows, provider="xendit", reference="PO-1"):
        lines = [{"number": index + 2, "order": order, "amount": Decimal(amount),
            "fee": Decimal(fee)} for index, (order, amount, fee) in enumerate(rows)]
        return import_payout(self.store, provider, reference,
            {"paid_on": date(2026, 11, 2), "lines": lines})


class CostOfGoodsTests(PayoutTestCase):
    def test_sale_books_cost_and_keeps_it_on_the_item(self):
        order = self._gateway_order("NK1")
        post_pending_entries()
        self.kopi.cost_price = 99999
        self.kopi.save()

        item = order.items.get()
        balances = _balances(self.store)
        self.assertEqual(item.unit_cost, Decimal(30000))
        self.assertEqual(balances[COGS], Decimal(60000))
        self.assertEqual(balances[INVENTORY], Decimal(-60000))

    def test_variant_cost_overrides_the_product_cost(self):
        variant = ProductVariant.objects.create(product=self.kopi, name="1kg", sku="KOPI-1KG",
            stock=5, cost_price=100000)
        order = self._gateway_order("NK2", quantity=1)
        order.items.update(variant=variant)
        post_pending_entries()

        self.assertEqual(_balances(self.store)[COGS], Decimal(100000))

    def test_products_without_cost_book_no_cost(self):
        self.kopi.cost_price = None
        self.kopi.save()
        self._gateway_order("NK3")
        post_pending_entries()

        self.assertEqual(_balances(self.store).get(COGS, 0), 0)


class PayoutImportTests(PayoutTestCase):
    def test_payout_moves_receivable_to_bank_and_books_fees(self):
        self._gateway_order("NK1")
        self._gateway_order("NK2", quantity=1)
        post_pending_entries()

        payout, created = self._payout([("NK1", 100000, 2000), ("x-NK2", 50000, 1000)])

        balances = _balances(self.store)
        self.assertTrue(created)
        self.assertEqual((payout.gross, payout.fees, payout.net),
            (Decimal(150000), Decimal(3000), Decimal(147000)))
        self.assertEqual(balances[GATEWAY], 0)
        self.assertEqual(balances[CASH], Decimal(147000))
        self.assertEqual(balances[FEES], Decimal(3000))
        self.assertEqual(outstanding_by_provider(self.store), [])

    def test_reimporting_a_payout_is_a_no_op(self):
        self._gateway_order("NK1")
        self._payout([("NK1", 100000, 0)])

        _, created = self._payout([("NK1", 100000, 0)])
        self.assertFalse(created)
        self.assertEqual(Payout.objects.count(), 1)

    def test_one_bad_row_rejects_the_whole_payout(self):
        self._gateway_order("NK1")
        with self.assertRaisesMessage(PayoutImportError, "tidak ditemukan"):
            self._payout([("NK1", 100000, 0), ("NOPE", 5000, 0)])

        self.assertFalse(Payout.objects.exists())
        self.assertEqual(outstanding_by_provider(self.store)[0]["count"], 1)

    def test_amount_must_match_the_payment(self):
        self._gateway_order("NK1")
        with self.assertRaisesMessage(PayoutImportError, "berbeda dari pembayaran"):
            self._payout([("NK1", 98000, 0)])

    def test_an_order_is_settled_only_once(self):
        self._gateway_order("NK1")
        self._payout([("NK1", 100000, 0)])

        with self.assertRaisesMessage(PayoutImportError, "sudah dicairkan"):
            self._payout([("NK1", 100000, 0)], reference="PO-2")

    def test_other_providers_and_stores_do_not_match(self):
        self._gateway_order("NK1", provider="doku")
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain")

        with self.assertRaises(PayoutImportError):
            self._payout([("NK1", 100000, 0)], provider="xendit")
        with self.assertRaises(PayoutImportError):
            import_payout(other, "doku", "PO-9", parse_payouts_csv(
                "payout_reference,payout_date,order_reference,amount\nPO-9,2026-11-02,NK1,100000\n"
            )["PO-9"])

    def test_marketplace_payout_settles_the_marketplace_receivable(self):
        channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="Shopee")
        import_order(channel, MarketplaceOrder(external_id="SHP-1", buyer_name="Budi", phone="1",
            address="x", lines=[OrderLine(sku="KOPI", quantity=1, unit_price=Decimal(50000))]))
        post_pending_entries()

        self._payout([("SHP-1", 50000, 4000)], provider="shopee")

        balances = _balances(self.store)
        self.assertEqual(balances[MARKETPLACE], 0)
        self.assertEqual(balances[CASH], Decimal(46000))

    def test_csv_groups_rows_and_reads_dates(self):
        grouped = parse_payouts_csv("payout_reference,payout_date,order_reference,amount,fee\n"
            "PO-1,02/11/2026,NK1,Rp 100.000,2000\nPO-1,02/11/2026,NK2,50000,\n"
            "PO-2,2026-11-03,NK3,1,0\n")

        self.assertEqual(list(grouped), ["PO-1", "PO-2"])
        self.assertEqual(grouped["PO-1"]["paid_on"].isoformat(), "2026-11-02")
        self.assertEqual([line["amount"] for line in grouped["PO-1"]["lines"]],
            [Decimal(100000), Decimal(50000)])
        with self.assertRaisesMessage(PayoutImportError, "Tanggal tidak valid"):
            parse_payouts_csv("payout_reference,payout_date,order_reference,amount\nA,kemarin,B,1\n")


class PayoutPageTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.owner)

    def test_upload_reports_created_and_rejected_payouts(self):
        self._gateway_order("NK1")
        csv = ("payout_reference,payout_date,order_reference,amount,fee\n"
            "PO-1,2026-11-02,NK1,100000,2500\nPO-2,2026-11-02,NOPE,1,0\n")

        page = self.client.post("/merchant/payouts/", {"provider": "xendit",
            "file": SimpleUploadedFile("p.csv", csv.encode(), content_type="text/csv")})

        self.assertEqual(page.context["summary"]["created"], 1)
        self.assertEqual(len(page.context["summary"]["errors"]), 1)
        self.assertContains(page, "Rp97.500")
        detail = self.client.get(f"/merchant/payouts/{Payout.objects.get().id}/")
        self.assertContains(detail, "NK1")

    def test_finance_page_shows_profit_and_loss(self):
        self._gateway_order("NK1")
        post_pending_entries()
        self._payout([("NK1", 100000, 2000)])

        page = self.client.get("/merchant/finance/")
        self.assertEqual(page.context["profit"]["gross"], Decimal(40000))
        self.assertEqual(page.context["profit"]["net"], Decimal(38000))
        self.assertContains(page, "Laba kotor")

    def test_other_sellers_cannot_open_a_payout(self):
        self._gateway_order("NK1")
        payout, _ = self._payout([("NK1", 100000, 0)])
        stranger = User.objects.create_user("stranger")
        Store.objects.create(owner=stranger, name="Lain", slug="lain")
        client = Client()
        client.force_login(stranger)

        self.assertEqual(client.get(f"/merchant/payouts/{payout.id}/").status_code, 404)


class ProductImportCostTests(TestCase):
    def test_cost_column_is_optional_and_blank_keeps_the_saved_cost(self):
        from apps.orders.setup_views import import_products, parse_import_rows

        store = Store.objects.create(owner=User.objects.create_user("o"), name="S", slug="s")
        row = {"name": "Kopi", "sku": "KOPI", "price": "50000", "stock": "1"}
        import_products(store, parse_import_rows([{**row, "cost_price": "30000"}]))
        import_products(store, parse_import_rows([{**row, "cost_price": ""}]))

        self.assertEqual(Product.objects.get(store=store).cost_price, Decimal(30000))
        with self.assertRaisesMessage(ValueError, "cost_price"):
            parse_import_rows([{**row, "cost_price": "abc"}])
