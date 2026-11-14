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


class FinishedOnlyImportTests(TestCase):
    """A marketplace file lists every stage: finished orders are sales, cancelled ones are recorded as
    cancelled, anything in between waits for a later file."""

    def setUp(self):
        owner = User.objects.create_user("finished")
        self.store = Store.objects.create(owner=owner, name="Toko", slug="toko-fin")
        self.kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi-fin", sku="KOPI",
            price=15000, cost_price=Decimal(9000))
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G1")
        self.level = InventoryLevel.objects.create(warehouse=warehouse, product=self.kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="tokopedia", name="Toko")

    def run_import(self, *orders):
        return import_orders(self.channel, list(orders), finished_only=True)

    def order(self, status, external_id, courier="", resi=""):
        return MarketplaceOrder(external_id=external_id, buyer_name="Budi", phone="0812",
            address="Bandung", status_text=status, courier=courier, tracking_number=resi,
            lines=[OrderLine(sku="KOPI", quantity=2, unit_price=Decimal(15000))])

    def test_finished_and_berhasil_are_sales_cancelled_is_recorded_the_rest_waits(self):
        summary = self.run_import(self.order("Selesai", "A"), self.order("Sedang dikirim", "B"),
            self.order("Perlu dikirim", "C"), self.order("Dibatalkan", "D"), self.order("Transaksi Berhasil", "E"))
        self.assertEqual((summary["created"], summary["not_finished"], summary["cancelled"]), (2, 2, 1))
        by_id = {o.external_order_id: o for o in Order.objects.filter(store=self.store)}
        self.assertEqual(set(by_id), {"A", "D", "E"})
        self.assertEqual((by_id["A"].status, by_id["D"].status, by_id["E"].status), ("completed", "cancelled", "completed"))
        self.assertEqual(by_id["E"].marketplace_status, "Transaksi Berhasil")
        self.level.refresh_from_db()
        self.assertEqual(self.level.on_hand, 46)                       # the cancelled order never took stock

    def test_the_resi_and_the_courier_are_kept_on_a_sale_and_on_a_cancellation(self):
        self.run_import(self.order("Selesai", "A", "J&T Express", "JY2130026621"),
            self.order("Dibatalkan", "D", "SPX Standard", "SPX999"))
        sale, called_off = Order.objects.get(external_order_id="A"), Order.objects.get(external_order_id="D")
        self.assertEqual((sale.shipping_service, sale.tracking_number), ("J&T Express", "JY2130026621"))
        self.assertEqual((called_off.shipping_service, called_off.tracking_number), ("SPX Standard", "SPX999"))

    def test_a_later_file_fills_in_the_resi_and_follows_the_status(self):
        self.import_early = import_orders(self.channel, [self.order("Perlu dikirim", "B")])      # no resi yet
        self.assertEqual(Order.objects.get(external_order_id="B").tracking_number, "")
        summary = self.run_import(self.order("Selesai", "B", "J&T Express", "JY1"))
        self.assertEqual(summary["created"], 0)
        late = Order.objects.get(external_order_id="B")
        self.assertEqual((late.status, late.marketplace_status, late.shipping_service, late.tracking_number),
            ("completed", "Selesai", "J&T Express", "JY1"))

    def test_an_unfinished_order_is_picked_up_by_a_later_file(self):
        self.run_import(self.order("Sedang dikirim", "B"))
        self.assertFalse(Order.objects.filter(store=self.store).exists())
        summary = self.run_import(self.order("Selesai", "B"))
        self.assertEqual(summary["created"], 1)

    def test_a_finished_order_that_is_later_returned_is_still_taken_back(self):
        self.run_import(self.order("Selesai", "A"))
        summary = self.run_import(self.order("Pengembalian", "A"))
        self.assertEqual(summary["not_finished"], 0)                    # known order: always brought up to date
        self.assertEqual(Order.objects.get(external_order_id="A").status, "returned")
        self.level.refresh_from_db()
        self.assertEqual(self.level.on_hand, 50)

    def test_without_the_filter_everything_is_still_imported(self):
        summary = import_orders(self.channel, [self.order("Sedang dikirim", "B")])
        self.assertEqual((summary["created"], summary["not_finished"]), (1, 0))

    def test_the_real_tokopedia_tiktok_export_follows_status_resi_and_courier(self):
        from apps.channels.parsers.tokopedia import parse_tokopedia_xlsx
        with open("docs/formattokped.xlsx", "rb") as sample:
            orders, skipped = parse_tokopedia_xlsx(sample)
        summary = import_orders(self.channel, orders, soft=True, skipped=skipped, finished_only=True)
        self.assertEqual((summary["created"], summary["cancelled"], summary["not_finished"], summary["errors"]),
            (3, 1, 2, []))                                       # 3 Selesai, 1 Dibatalkan, 2 Dikirim waiting
        done = Order.objects.filter(store=self.store, status="completed")
        self.assertEqual(done.count(), 3)
        self.assertEqual(sorted(done.values_list("shipping_service", flat=True)), ["Grab", "J&T Express", "J&T Express"])
        self.assertTrue(all(done.values_list("tracking_number", flat=True)))
        self.assertEqual(Order.objects.get(store=self.store, status="cancelled").marketplace_status, "Dibatalkan")

    def test_a_finished_order_is_recorded_even_when_the_stock_is_short(self):
        short = Product.objects.create(store=self.store, name="Sepatu", slug="sepatu", sku="SEPATU", price=99000)
        data = MarketplaceOrder(external_id="Z1", buyer_name="Budi", phone="0812", address="Bandung",
            status_text="Selesai", courier="J&T Express", tracking_number="JY9",
            lines=[OrderLine(sku="SEPATU", quantity=1, unit_price=Decimal(99000))])
        summary = import_orders(self.channel, [data], finished_only=True)       # strict mode, no stock at all
        self.assertEqual((summary["created"], summary["no_stock"], summary["errors"]), (1, 1, []))
        order = Order.objects.get(external_order_id="Z1")
        self.assertEqual((order.status, order.tracking_number, order.shipping_service), ("completed", "JY9", "J&T Express"))
        self.assertIn("Stok SKU SEPATU kurang", order.channel_order.attention)
        self.assertFalse(InventoryLevel.objects.filter(product=short).exists())      # no stock was taken

    def test_a_short_order_that_is_not_finished_still_fails_in_strict_mode(self):
        Product.objects.create(store=self.store, name="Sepatu", slug="sepatu", sku="SEPATU", price=99000)
        data = MarketplaceOrder(external_id="Z2", buyer_name="Budi", phone="0812", address="Bandung",
            status_text="Perlu dikirim", lines=[OrderLine(sku="SEPATU", quantity=1, unit_price=Decimal(99000))])
        summary = import_orders(self.channel, [data])
        self.assertEqual((summary["created"], len(summary["errors"])), (0, 1))

    def test_a_rebuilt_buyer_profile_reuses_its_customer_instead_of_crashing(self):
        from apps.channels.models import ChannelBuyer
        from apps.shops.models import Customer
        def sale(external_id):
            return MarketplaceOrder(external_id=external_id, buyer_name="Ani", phone="0812", address="Bandung",
                status_text="Selesai", raw={"tokopedia_username": "ani123"},
                lines=[OrderLine(sku="KOPI", quantity=1, unit_price=Decimal(15000))])
        import_orders(self.channel, [sale("R1")], finished_only=True)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)
        ChannelBuyer.objects.filter(store=self.store).delete()                 # the clean-up that used to break it
        summary = import_orders(self.channel, [sale("R2")], finished_only=True)
        self.assertEqual((summary["created"], summary["errors"]), (1, []))
        customer, = Customer.objects.filter(store=self.store)
        self.assertEqual(customer.channel_buyer.external_id, "ani123")

    def test_the_real_shopee_export_with_empty_skus_is_fully_imported(self):
        """Order.all (Shopee): 3 Selesai and 2 Batal, no SKU anywhere, courier inside Opsi Pengiriman."""
        from apps.channels.parsers.shopee import parse_shopee_xlsx
        shopee = SalesChannel.objects.create(store=self.store, platform="shopee", name="Toko Shopee")
        with open("docs/Order.all.20251201_20251231.xlsx", "rb") as sample:
            orders, skipped = parse_shopee_xlsx(sample)
        summary = import_orders(shopee, orders, skipped=skipped, finished_only=True)          # strict mode
        self.assertEqual((summary["created"], summary["cancelled"], summary["not_finished"], summary["errors"]),
            (3, 2, 0, []))
        self.assertEqual(summary["new_products"] + summary["no_stock"], 3)
        done = Order.objects.filter(store=self.store, channel="shopee", status="completed")
        self.assertEqual(done.count(), 3)
        self.assertEqual({o.external_order_id: (o.shipping_service, o.tracking_number, o.total) for o in done}, {
            "251202A5J14X0H": ("JNE Reguler", "CM05459265352", Decimal(699000)),
            "251202AEJPENKP": ("SiCepat REG", "004535364538", Decimal(599000)),
            "2512101WKAXU23": ("JNE YES", "JY91008038038", Decimal(737000))})        # 699.000 + 38.000 shipping
        gone = Order.objects.filter(store=self.store, channel="shopee", status="cancelled")
        self.assertEqual({(o.shipping_service, o.tracking_number) for o in gone},
            {("Ninja Xpress", "SHP3416253009"), ("Ninja Xpress", "SHP7367041320")})
        self.assertTrue(all(o.channel_order.attention for o in done))                        # flagged for SKU mapping
        self.assertEqual(sorted(set(done.values_list("marketplace_status", flat=True))), ["Selesai"])

    def test_an_unknown_sku_still_fails_for_an_order_that_is_not_finished(self):
        data = MarketplaceOrder(external_id="U1", buyer_name="Budi", phone="0812", address="Bandung",
            status_text="Perlu dikirim", lines=[OrderLine(sku="ADA-TIDAK", quantity=1, unit_price=Decimal(1000))])
        summary = import_orders(self.channel, [data])
        self.assertEqual((summary["created"], len(summary["errors"])), (0, 1))
