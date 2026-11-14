"""
Parse a Lazada Seller Center order export (XLSX or CSV) into MarketplaceOrder objects.

Lazada writes one row per unit sold, grouped by ``orderNumber``. Rows of one order with the same
SKU and price become a single line with a quantity. Headers are matched ignoring case, spaces and
punctuation, because Lazada's exports differ by country and over time. If one item of an order
is cancelled while others stand, only the standing items are imported; an order with every item
cancelled is imported as cancelled.
"""
import csv
import io
import re
from collections import OrderedDict
from decimal import Decimal

import openpyxl

from apps.channels.adapters import (
    STATE_CANCELLED,
    STATE_UNPAID,
    MarketplaceOrder,
    OrderLine,
    normalize_state,
    parse_marketplace_time,
)
from apps.channels.services import ChannelImportError, parse_amount

# key -> header names it may carry, normalised (lowercase letters and digits only).
_COLS = {
    "order_id": ["ordernumber", "orderid", "nomorpesanan", "nopesanan"],
    "status": ["status", "orderstatus", "statuspesanan"],
    "sku": ["sellersku", "skupenjual", "sku"],
    "name": ["itemname", "productname", "namaproduk", "namabarang"],
    "variation": ["variation", "variasi"],
    "quantity": ["quantity", "qty", "jumlah"],
    "unit_price": ["unitprice", "hargasatuan", "hargaproduk"],
    "paid_price": ["paidprice", "hargadibayar"],
    "seller_discount": ["sellerdiscounttotal", "sellerdiscount", "diskonpenjual"],
    "shipping_fee": ["shippingfee", "ongkir", "ongkoskirim", "biayapengiriman"],
    "courier": ["shippingprovider", "cdshippingprovider", "kurir", "ekspedisi"],
    "tracking": ["trackingcode", "cdtrackingcode", "noresi", "nomorresi"],
    "customer": ["customername", "namapelanggan", "namapembeli"],
    "recipient": ["shippingname", "namapenerima"],
    "phone": ["shippingphone", "notelepon", "telepon"],
    "address": ["shippingaddress", "alamatpengiriman", "alamat"],
    "city": ["shippingcity", "kota"],
    "province": ["shippingregion", "provinsi"],
    "created": ["createtime", "createdat", "ordercreatedat", "tanggalpesanan", "waktupesanan"],
    "paid": ["paidtime", "paymenttime", "waktupembayaran"],
}
_REQUIRED = {"order_id", "unit_price"}


def _norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _index(header_row):
    names = {_norm(cell): number for number, cell in reversed(list(enumerate(header_row)))}
    index = {key: names[option] for key, options in _COLS.items() for option in options
        if option in names}
    missing = _REQUIRED - set(index)
    if missing:
        raise ChannelImportError("Kolom wajib Lazada tidak ditemukan: "
            + ", ".join(sorted(missing)) + ". Pastikan file adalah ekspor pesanan Lazada.")
    return index


def _rows(upload):
    name = getattr(upload, "name", "") or ""
    if name.lower().endswith(".csv"):
        text = upload.read().decode("utf-8-sig") if hasattr(upload, "read") else upload
        return [row for row in csv.reader(io.StringIO(text))]
    try:
        workbook = openpyxl.load_workbook(upload, data_only=True)
    except Exception as exc:
        raise ChannelImportError(f"File tidak dapat dibaca sebagai Excel: {exc}") from exc
    return [list(row) for row in workbook.active.iter_rows(values_only=True)]


def _get(row, index, key, default=""):
    position = index.get(key)
    if position is None or position >= len(row) or row[position] is None:
        return default
    return str(row[position]).strip()


def _money(row, index, key):
    value = _get(row, index, key)
    return abs(parse_amount(value)) if value else Decimal(0)


def parse_lazada(upload):
    """Returns (list[MarketplaceOrder], skipped_count): skipped are orders nobody paid for."""
    rows = _rows(upload)
    if len(rows) < 2:
        raise ChannelImportError("File kosong (tidak ada data pesanan).")
    index = _index(rows[0])
    grouped = OrderedDict()
    for number, row in enumerate(rows[1:], start=2):
        order_id = _get(row, index, "order_id")
        if not order_id:
            continue
        try:
            quantity = int(parse_amount(_get(row, index, "quantity") or "1")) or 1
            price = _money(row, index, "unit_price")
            if not price and _get(row, index, "paid_price"):
                price = _money(row, index, "paid_price")
            discount, shipping = _money(row, index, "seller_discount"), _money(
                row, index, "shipping_fee")
        except ChannelImportError as exc:
            raise ChannelImportError(f"Baris {number}: {exc}") from exc
        head = grouped.setdefault(order_id, {"row": row, "items": []})
        head["items"].append({"status": _get(row, index, "status"), "quantity": quantity,
            "price": price, "discount": discount, "shipping": shipping, "row": row})
    orders, skipped = [], 0
    for order_id, head in grouped.items():
        items = head["items"]
        states = {normalize_state(item["status"]) for item in items}
        if states == {STATE_UNPAID}:
            skipped += 1
            continue
        standing = [item for item in items if normalize_state(item["status"]) != STATE_CANCELLED]
        items = standing or items  # an order with every item cancelled keeps them all
        lines = OrderedDict()
        for item in items:
            row = item["row"]
            sku = _get(row, index, "sku") or "[Tanpa SKU: {}]".format(
                (_get(row, index, "name") + " " + _get(row, index, "variation")).strip()[:47])
            key = (sku, item["price"])
            if key in lines:
                lines[key].quantity += item["quantity"]
            else:
                lines[key] = _Line(sku, item["quantity"], item["price"],
                    _get(row, index, "name"))
        first = head["row"]
        buyer = _get(first, index, "customer") or _get(first, index, "recipient")
        status = items[0]["status"]
        orders.append(MarketplaceOrder(
            external_id=order_id[:80], buyer_name=buyer or "Pembeli Lazada",
            phone=_get(first, index, "phone") or "-",
            address=", ".join(part for part in [_get(first, index, "address"),
                _get(first, index, "city"), _get(first, index, "province")] if part) or "-",
            lines=[OrderLine(sku=line.sku, quantity=line.quantity, unit_price=line.price,
                name=line.name) for line in lines.values()],
            shipping_cost=sum((item["shipping"] for item in items), Decimal(0)),
            discount=sum((item["discount"] for item in items), Decimal(0)),
            courier=_get(first, index, "courier"), tracking_number=_get(first, index, "tracking"),
            status_text=status,
            placed_at=parse_marketplace_time(first[index["created"]]) if "created" in index else None,
            paid_at=parse_marketplace_time(first[index["paid"]]) if "paid" in index else None,
            raw={"lazada_status": status, "buyer_username": buyer,
                "city": _get(first, index, "city"), "province": _get(first, index, "province")}))
    return orders, skipped


class _Line:
    def __init__(self, sku, quantity, price, name):
        self.sku, self.quantity, self.price, self.name = sku, quantity, price, name
