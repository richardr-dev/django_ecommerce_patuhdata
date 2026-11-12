"""TikTok Shop and Lazada exports import as orders, with their statuses."""
import io
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels.models import SalesChannel
from apps.channels.parsers import parse_order_file
from apps.channels.parsers.lazada import parse_lazada
from apps.channels.parsers.tokopedia import _COLS
from apps.channels.services import ChannelImportError, import_orders
from apps.orders.models import Order
from apps.shops.models import Store


def _workbook(header, rows, extra_header_row=None):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(header)
    if extra_header_row:
        sheet.append(extra_header_row)
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    stream.seek(0)
    stream.name = "export.xlsx"
    return stream


LAZADA_HEADER = ["orderItemId", "orderNumber", "sellerSku", "itemName", "variation", "unitPrice",
    "sellerDiscountTotal", "shippingFee", "status", "shippingProvider", "trackingCode",
    "customerName", "shippingPhone", "shippingAddress", "shippingCity", "shippingRegion"]


def _lz(item, number, sku, price, status="delivered", discount=0, shipping=0):
    return [item, number, sku, "Kopi", "", price, discount, shipping, status, "J&T", "JT1",
        "Sari", "0812***", "Jl. Mawar", "Bandung", "Jawa Barat"]


class LazadaTests(TestCase):
    def test_one_row_per_unit_becomes_one_line_with_a_quantity(self):
        orders, skipped = parse_lazada(_workbook(LAZADA_HEADER, [
            _lz(1, "LZ1", "KOPI", 50000, discount=2000, shipping=5000),
            _lz(2, "LZ1", "KOPI", 50000, discount=2000, shipping=5000),
            _lz(3, "LZ1", "TEH", 20000)]))

        order, = orders
        self.assertEqual(skipped, 0)
        self.assertEqual([(line.sku, line.quantity, line.unit_price) for line in order.lines],
            [("KOPI", 2, Decimal(50000)), ("TEH", 1, Decimal(20000))])
        self.assertEqual((order.discount, order.shipping_cost), (Decimal(4000), Decimal(10000)))
        self.assertEqual((order.state, order.buyer_name, order.courier), ("completed", "Sari",
            "J&T"))

    def test_statuses_cancelled_items_and_unpaid_orders(self):
        orders, skipped = parse_lazada(_workbook(LAZADA_HEADER, [
            _lz(1, "A", "KOPI", 50000, status="canceled"),
            _lz(2, "A", "TEH", 20000, status="shipped"),
            _lz(3, "B", "KOPI", 50000, status="canceled"),
            _lz(4, "C", "KOPI", 50000, status="returned"),
            _lz(5, "D", "KOPI", 50000, status="unpaid")]))

        by_id = {order.external_id: order for order in orders}
        # One cancelled item does not cancel the order: only the standing item is imported.
        self.assertEqual([line.sku for line in by_id["A"].lines], ["TEH"])
        self.assertEqual(by_id["A"].state, "shipped")
        self.assertEqual(by_id["B"].state, "cancelled")
        self.assertEqual(by_id["C"].state, "returned")
        self.assertEqual((skipped, "D" in by_id), (1, False))

    def test_csv_and_a_wrong_file(self):
        header = ",".join(LAZADA_HEADER)
        row = ",".join(str(cell) for cell in _lz(1, "LZ9", "KOPI", 50000))
        upload = SimpleUploadedFile("lazada.csv", f"{header}\n{row}\n".encode())

        orders, _ = parse_order_file("lazada", upload)
        self.assertEqual(orders[0].external_id, "LZ9")
        with self.assertRaises(ChannelImportError):
            parse_lazada(_workbook(["Nama", "Alamat"], [["a", "b"]]))


def _tiktok_header():
    return list(_COLS.values())


def _tt(order, status, sku, quantity=1, return_type="", return_qty=0):
    values = {"Order ID": order, "Order Status": status, "Seller SKU": sku,
        "Product Name": "Kopi", "Quantity": quantity, "SKU Unit Original Price": 50000,
        "SKU Seller Discount": 0, "Shipping Fee After Discount": 0, "Buyer Username": "buyer1",
        "Cancelation/Return Type": return_type, "Sku Quantity of return": return_qty,
        "Regency and City": "Bandung", "Province": "Jawa Barat", "Detail Address": "Jl. X"}
    return [values.get(name, "") for name in _tiktok_header()]


class TikTokShopTests(TestCase):
    def _parse(self, rows):
        return parse_order_file("tiktok_shop", _workbook(_tiktok_header(), rows,
            extra_header_row=["desc"] * len(_tiktok_header())))

    def test_tiktok_shop_uses_the_seller_center_export(self):
        orders, skipped = self._parse([_tt("T1", "Completed", "KOPI", 2),
            _tt("T2", "Cancelled", "KOPI"), _tt("T3", "Unpaid", "KOPI")])

        self.assertEqual({o.external_id: o.state for o in orders},
            {"T1": "completed", "T2": "cancelled"})
        self.assertEqual(skipped, 1)

    def test_a_fully_returned_order_is_a_return_but_a_part_return_stays_a_sale(self):
        orders, _ = self._parse([
            _tt("R1", "Completed", "KOPI", 2, "Return/Refund", 2),
            _tt("R2", "Completed", "KOPI", 3, "Return/Refund", 1)])

        self.assertEqual({o.external_id: o.state for o in orders},
            {"R1": "returned", "R2": "completed"})


class EndToEndTests(TestCase):
    def test_a_lazada_file_imports_and_a_later_cancellation_returns_stock(self):
        owner = User.objects.create_user("lz-owner")
        store = Store.objects.create(owner=owner, name="Toko", slug="toko-lz")
        gudang = Warehouse.objects.create(store=store, name="G", code="G1")
        kopi = Product.objects.create(store=store, name="Kopi", slug="kopi", sku="KOPI", price=50000)
        level = InventoryLevel.objects.create(warehouse=gudang, product=kopi, on_hand=10)
        channel = SalesChannel.objects.create(store=store, platform="lazada", name="Lazada")

        orders, _ = parse_order_file("lazada", _workbook(LAZADA_HEADER, [
            _lz(1, "LZ1", "KOPI", 50000, status="shipped"),
            _lz(2, "LZ1", "KOPI", 50000, status="shipped")]))
        import_orders(channel, orders)
        level.refresh_from_db()
        self.assertEqual(level.on_hand, 8)

        later, _ = parse_order_file("lazada", _workbook(LAZADA_HEADER, [
            _lz(1, "LZ1", "KOPI", 50000, status="canceled"),
            _lz(2, "LZ1", "KOPI", 50000, status="canceled")]))
        summary = import_orders(channel, later)

        level.refresh_from_db()
        self.assertEqual((level.on_hand, summary["updated"], summary["cancelled"]), (10, 1, 1))
        self.assertEqual(Order.objects.get().status, "cancelled")


class OrderTimeTests(TestCase):
    """The export's own order and payment time is read, so the sale is dated by it."""

    def test_the_time_formats_the_marketplaces_use(self):
        import datetime

        from django.utils import timezone

        from apps.channels.adapters import parse_marketplace_time
        local = timezone.get_current_timezone()
        expected = datetime.datetime(2025, 12, 15, 14, 30, 22, tzinfo=local)
        self.assertEqual(parse_marketplace_time("15/12/2025 14:30:22"), expected)       # TikTok/Tokopedia
        self.assertEqual(parse_marketplace_time("2025-12-15 14:30:22"), expected)       # Shopee
        self.assertEqual(parse_marketplace_time(datetime.datetime(2025, 12, 15, 14, 30, 22)), expected)
        self.assertEqual(parse_marketplace_time("2025-12-15").date(), datetime.date(2025, 12, 15))
        self.assertIsNone(parse_marketplace_time(""))
        self.assertIsNone(parse_marketplace_time("bukan tanggal"))
        self.assertIsNone(parse_marketplace_time(None))

    def test_tiktok_shop_export_carries_created_and_paid_time(self):
        import datetime
        header = _tiktok_header()
        row = {name: "" for name in header}
        row.update({_COLS["order_id"]: "586865196962908050", _COLS["status"]: "Selesai",
            _COLS["seller_sku"]: "KOPI", _COLS["product_name"]: "Kopi", _COLS["quantity"]: 2,
            _COLS["unit_price"]: 15000, _COLS["created"]: "15/12/2025 14:30:22",
            _COLS["paid"]: "15/12/2025 14:35:00", _COLS["buyer_username"]: "budi"})
        upload = _workbook(header, [[row[name] for name in header]],
            extra_header_row=["desc"] * len(header))
        orders, _ = parse_order_file("tokopedia", upload)
        placed = orders[0].placed_at
        self.assertEqual((placed.date(), placed.hour, placed.minute),
            (datetime.date(2025, 12, 15), 14, 30))
        self.assertEqual(orders[0].paid_at.minute, 35)

    def test_shopee_export_carries_the_order_time_when_the_column_is_there(self):
        import datetime

        from apps.channels.parsers.shopee import _COLS as SHOPEE
        header = list(SHOPEE.values())
        row = {name: "" for name in header}
        row.update({SHOPEE["order_id"]: "2512018689187H", SHOPEE["status"]: "Selesai",
            SHOPEE["product_name"]: "Kopi", SHOPEE["sku_ref"]: "KOPI", SHOPEE["unit_price"]: 15000,
            SHOPEE["quantity"]: 1, SHOPEE["username"]: "budi",
            SHOPEE["created"]: datetime.datetime(2025, 12, 1, 9, 5, 0),
            SHOPEE["paid"]: "2025-12-01 09:07"})
        upload = _workbook(header, [[row[name] for name in header]])
        orders, _ = parse_order_file("shopee", upload)
        self.assertEqual(orders[0].placed_at.date(), datetime.date(2025, 12, 1))
        self.assertEqual((orders[0].paid_at.hour, orders[0].paid_at.minute), (9, 7))

    def test_a_shopee_file_without_the_column_still_imports_with_no_time(self):
        from apps.channels.parsers.shopee import _COLS as SHOPEE
        header = [SHOPEE[k] for k in ("order_id", "status", "product_name", "sku_ref", "unit_price",
            "quantity", "username")]
        upload = _workbook(header, [["2512018689187H", "Selesai", "Kopi", "KOPI", 15000, 1, "budi"]])
        orders, _ = parse_order_file("shopee", upload)
        self.assertIsNone(orders[0].placed_at)
