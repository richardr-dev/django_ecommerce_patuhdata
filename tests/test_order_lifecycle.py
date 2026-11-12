"""A marketplace order keeps changing after it is imported; the books and the stock follow."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounting.ledger import (
    INVENTORY,
    MARKETPLACE,
    SALES,
    account_balances,
    post_backlog,
    post_pending_entries,
)
from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.channels.adapters import MarketplaceOrder, OrderLine, normalize_state
from apps.channels.models import ImportBatch, SalesChannel
from apps.channels.services import import_order, import_order_outcome, import_orders
from apps.orders.models import InventoryMovement, Order
from apps.shops.models import Store
from apps.tax.models import Invoice


def _order(status="", external_id="SHP-1", quantity=2, sku="KOPI"):
    return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812",
        address="Bandung", status_text=status,
        lines=[OrderLine(sku=sku, quantity=quantity, unit_price=Decimal(15000))])


def _balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class StateTests(TestCase):
    def test_labels_in_both_languages_map_to_one_state(self):
        for text, state in [("Selesai", "completed"), ("Completed", "completed"),
                ("Batal", "cancelled"), ("Pesanan Dibatalkan", "cancelled"),
                ("Cancelled", "cancelled"), ("Pengembalian", "returned"),
                ("Return/Refund", "returned"), ("Dikirim", "shipped"),
                ("Belum Bayar", "unpaid"), ("Perlu Dikirim", "shipped"), ("", "active"),
                ("Status baru", "active")]:
            self.assertEqual(normalize_state(text), state, text)


class LifecycleTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("lifecycle")
        self.store = Store.objects.create(owner=owner, name="Toko", slug="toko-lc")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=15000, cost_price=Decimal(9000))
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi,
            on_hand=10)
        self.teh = Product.objects.create(store=self.store, name="Teh", slug="teh", sku="TEH",
            price=20000)
        self.teh_m = ProductVariant.objects.create(product=self.teh, name="M", sku="TEH-M",
            stock=4)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee",
            name="Toko Shopee")

    def stock(self):
        self.level.refresh_from_db()
        return self.level.on_hand

    def test_cancelling_after_import_returns_stock_and_clears_the_books(self):
        order, _ = import_order(self.channel, _order("Dikirim"))
        post_pending_entries()
        self.assertEqual(self.stock(), 8)
        self.assertEqual(_balance(self.store, SALES), Decimal(30000))

        import_order(self.channel, _order("Batal"))

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.marketplace_status, "Batal")
        self.assertEqual(self.stock(), 10)
        for code in (SALES, MARKETPLACE, INVENTORY):
            self.assertEqual(_balance(self.store, code), 0)
        self.assertEqual(Invoice.objects.get().status, Invoice.STATUS_VOID)
        self.assertEqual(post_backlog() + post_pending_entries(), 0)

    def test_cancelling_before_the_worker_booked_the_sale_leaves_nothing(self):
        import_order(self.channel, _order())

        import_order(self.channel, _order("Batal"))
        post_pending_entries()

        self.assertEqual(_balance(self.store, SALES), 0)
        self.assertEqual(self.stock(), 10)

    def test_a_cancellation_is_applied_once(self):
        import_order(self.channel, _order())
        import_order(self.channel, _order("Batal"))
        import_order(self.channel, _order("Batal"))

        self.assertEqual(self.stock(), 10)
        self.assertEqual(InventoryMovement.objects.filter(kind="return").count(), 1)

    def test_return_restocks_a_variant(self):
        import_order(self.channel, _order(sku="TEH-M", quantity=3))
        self.teh_m.refresh_from_db()
        self.assertEqual(self.teh_m.stock, 1)

        import_order(self.channel, _order("Pengembalian", sku="TEH-M", quantity=3))

        self.teh_m.refresh_from_db()
        self.assertEqual(self.teh_m.stock, 4)
        self.assertEqual(Order.objects.get().status, "returned")

    def test_status_moves_forward_only(self):
        order, _ = import_order(self.channel, _order())
        import_order(self.channel, _order("Dikirim"))
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

        import_order(self.channel, _order("Selesai"))
        import_order(self.channel, _order("Dikirim"))

        order.refresh_from_db()
        self.assertEqual(order.status, "completed")

    def test_a_cancelled_order_never_comes_back(self):
        import_order(self.channel, _order("Batal"))
        import_order(self.channel, _order("Selesai"))

        self.assertEqual(Order.objects.get().status, "cancelled")

    def test_never_seen_cancelled_order_is_recorded_without_a_sale(self):
        order, created = import_order(self.channel, _order("Batal"))
        post_pending_entries()

        self.assertTrue(created)
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.stock(), 10)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(_balance(self.store, SALES), 0)
        self.assertFalse(order.payment_transactions.exists())

    def test_batch_summary_counts_updates_and_cancellations(self):
        import_order(self.channel, _order(external_id="A"))
        import_order(self.channel, _order(external_id="B"))

        summary = import_orders(self.channel, [_order("Batal", "A"), _order("Selesai", "B"),
            _order("Batal", "C"), _order("", "D")])

        self.assertEqual((summary["created"], summary["updated"], summary["cancelled"],
            summary["duplicates"]), (1, 2, 2, 0))
        batch = ImportBatch.objects.get(pk=summary["batch_id"])
        self.assertEqual((batch.updated_count, batch.cancelled_count), (2, 2))


class MarketplaceDateTests(TestCase):
    """A sale belongs to the day the buyer ordered it on the marketplace, not the day the file
    was imported: the books, the invoice and the months all follow."""

    def setUp(self):
        owner = User.objects.create_user("dates")
        self.store = Store.objects.create(owner=owner, name="Toko", slug="toko-dt")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=15000, cost_price=Decimal(9000))
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="tokopedia",
            name="Toko Tokopedia")

    def placed(self, external_id="TKP-1", day=(2025, 12, 15), **extra):
        import datetime

        from django.utils import timezone
        when = timezone.make_aware(datetime.datetime(*day, 14, 30))
        data = MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812",
            address="Bandung", status_text="Selesai", placed_at=when, paid_at=extra.pop("paid_at", when),
            lines=[OrderLine(sku="KOPI", quantity=2, unit_price=Decimal(15000))])
        return data

    def test_order_payment_invoice_and_ledger_take_the_marketplace_date(self):
        import datetime
        order, created = import_order(self.channel, self.placed())
        self.assertTrue(created)
        post_pending_entries()
        from django.utils import timezone
        self.assertEqual(timezone.localdate(order.created_at), datetime.date(2025, 12, 15))
        payment = order.payment_transactions.get()
        self.assertEqual(timezone.localdate(payment.updated_at), datetime.date(2025, 12, 15))
        self.assertEqual(Invoice.objects.get(order=order).issued_on, datetime.date(2025, 12, 15))
        from apps.accounting.models import JournalEntry
        entry = JournalEntry.objects.get(source_type="payment", source_id=str(payment.pk))
        self.assertEqual(entry.date, datetime.date(2025, 12, 15))
        self.assertEqual(_balance(self.store, SALES), Decimal(30000))

    def test_the_month_report_counts_it_in_december_not_in_the_import_month(self):
        import datetime

        from apps.accounting.ledger import account_balances
        import_order(self.channel, self.placed())
        post_pending_entries()
        december = account_balances(self.store, start=datetime.date(2025, 12, 1),
            end=datetime.date(2025, 12, 31))
        self.assertEqual({a.code: a.balance for a in december}[SALES], Decimal(30000))
        today_month = account_balances(self.store, start=datetime.date(2026, 1, 1),
            end=datetime.date(2030, 1, 1))
        self.assertEqual({a.code: a.balance for a in today_month}.get(SALES, 0), 0)

    def test_an_order_without_a_time_in_the_file_keeps_the_import_day(self):
        from django.utils import timezone
        data = MarketplaceOrder(external_id="TKP-2", buyer_name="Ani", phone="08", address="x",
            status_text="Selesai", lines=[OrderLine(sku="KOPI", quantity=1, unit_price=Decimal(15000))])
        order, _ = import_order(self.channel, data)
        self.assertEqual(timezone.localdate(order.created_at), timezone.localdate())

    def test_reimporting_the_file_corrects_orders_that_were_booked_on_the_import_day(self):
        import datetime

        from django.utils import timezone

        from apps.accounting.models import JournalEntry
        wrong = MarketplaceOrder(external_id="TKP-3", buyer_name="Budi", phone="0812",
            address="Bandung", status_text="Selesai",
            lines=[OrderLine(sku="KOPI", quantity=2, unit_price=Decimal(15000))])
        order, _ = import_order(self.channel, wrong)   # an earlier import without the order time
        post_pending_entries()
        payment = order.payment_transactions.get()
        before = JournalEntry.objects.get(source_type="payment", source_id=str(payment.pk),
            superseded=False)
        self.assertEqual(before.date, timezone.localdate())
        same, outcome = import_order_outcome(self.channel, self.placed("TKP-3"))
        self.assertEqual((same.pk, outcome), (order.pk, "updated"))
        live = JournalEntry.objects.get(source_type="payment", source_id=str(payment.pk),
            superseded=False)
        self.assertEqual(live.date, datetime.date(2025, 12, 15))
        self.assertTrue(JournalEntry.objects.get(pk=before.pk).superseded)   # reversed, not edited
        self.assertEqual(_balance(self.store, SALES), Decimal(30000))        # booked once
        self.assertEqual(Invoice.objects.get(order=order).issued_on, datetime.date(2025, 12, 15))
        _, again = import_order_outcome(self.channel, self.placed("TKP-3"))
        self.assertEqual(again, "duplicate")                                  # nothing left to fix

    def test_a_cancelled_order_keeps_its_date_too(self):
        import datetime

        from django.utils import timezone
        data = self.placed("TKP-4")
        cancelled = MarketplaceOrder(external_id="TKP-4", buyer_name="Budi", phone="08",
            address="x", status_text="Dibatalkan", placed_at=data.placed_at, lines=data.lines)
        order, _ = import_order(self.channel, cancelled)
        self.assertEqual(timezone.localdate(order.created_at), datetime.date(2025, 12, 15))
        self.assertEqual(order.status, "cancelled")


class InvoiceNeverLostTests(MarketplaceDateTests):
    def test_when_invoicing_fails_during_import_the_order_stays_and_the_worker_issues_it_later(self):
        import datetime
        from unittest import mock

        from apps.accounting.ledger import post_everything
        with mock.patch("apps.channels.services.issue_invoice_for_order",
                side_effect=RuntimeError("boom")):
            order, created = import_order(self.channel, self.placed("TKP-9"))
        self.assertTrue(created)
        self.assertFalse(Invoice.objects.filter(order=order).exists())
        post_everything()
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.issued_on, datetime.date(2025, 12, 15))   # dated by the order
        self.assertEqual(invoice.status, "paid")
        self.assertEqual(invoice.total, Decimal(30000))
        post_everything()
        self.assertEqual(Invoice.objects.filter(order=order).count(), 1)    # once

    def test_cancelled_marketplace_orders_never_get_an_invoice(self):
        from apps.accounting.ledger import post_everything
        data = self.placed("TKP-10")
        cancelled = MarketplaceOrder(external_id="TKP-10", buyer_name="B", phone="08", address="x",
            status_text="Dibatalkan", placed_at=data.placed_at, lines=data.lines)
        order, _ = import_order(self.channel, cancelled)
        post_everything()
        self.assertFalse(Invoice.objects.filter(order=order).exists())
