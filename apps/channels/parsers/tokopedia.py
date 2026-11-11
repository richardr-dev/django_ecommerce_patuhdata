"""
Parse Tokopedia Seller Center XLSX exports into MarketplaceOrder objects.

Tokopedia exports have two header rows: row 1 is column names, row 2 is
descriptions. Data starts at row 3. Phone numbers, names, and addresses are
masked by Tokopedia. Buyer Username (also partially masked) is used as the
ChannelBuyer external_id. Cancelled orders are silently skipped.
"""
from collections import OrderedDict
from decimal import Decimal

import openpyxl

from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.services import ChannelImportError, parse_amount

_COLS = {
    "order_id":        "Order ID",
    "status":          "Order Status",
    "seller_sku":      "Seller SKU",
    "product_name":    "Product Name",
    "variation":       "Variation",
    "quantity":        "Quantity",
    "unit_price":      "SKU Unit Original Price",
    "seller_discount": "SKU Seller Discount",
    "shipping_fee":    "Shipping Fee After Discount",
    "tracking":        "Tracking ID",
    "courier":         "Shipping Provider Name",
    "buyer_username":  "Buyer Username",
    "recipient":       "Recipient",
    "phone":           "Phone #",
    "province":        "Province",
    "city":            "Regency and City",
    "address":         "Detail Address",
}

_SKIP_STATUSES = {"Dibatalkan", "Cancelled", "Return/Refund"}

_REQUIRED_KEYS = {"order_id", "product_name", "quantity", "unit_price"}


def _build_col_index(header_row):
    name_to_idx = {str(v or "").strip(): i for i, v in enumerate(header_row)}
    index = {key: name_to_idx[col] for key, col in _COLS.items() if col in name_to_idx}
    missing = _REQUIRED_KEYS - set(index)
    if missing:
        col_names = ", ".join(_COLS[k] for k in missing)
        raise ChannelImportError(
            f"File bukan format ekspor Tokopedia. Kolom wajib tidak ditemukan: {col_names}"
        )
    return index


def _get(row, index, key, default=""):
    idx = index.get(key)
    if idx is None:
        return default
    val = row[idx]
    if val is None:
        return default
    return str(val).strip()


def _sku(row, index):
    val = _get(row, index, "seller_sku")
    if val:
        return val
    name = _get(row, index, "product_name")
    variation = _get(row, index, "variation")
    display = f"{name} {variation}".strip()[:50]
    return f"[Tanpa SKU: {display}]"


def _buyer_name(row, index):
    name = _get(row, index, "recipient")
    if name and "*" not in name:
        return name
    username = _get(row, index, "buyer_username")
    return f"@{username}" if username else "Pembeli Tokopedia"


def _phone(row, index):
    # Tokopedia always masks phone. Use buyer username as identifier.
    return _get(row, index, "buyer_username") or "-"


def _address(row, index):
    parts = [p for p in [
        _get(row, index, "address"),
        _get(row, index, "city"),
        _get(row, index, "province"),
    ] if p]
    return ", ".join(parts) or "-"


def parse_tokopedia_xlsx(upload):
    """Parse a Tokopedia Seller Center XLSX export.

    Returns (list[MarketplaceOrder], skipped_count).
    """
    try:
        wb = openpyxl.load_workbook(upload, data_only=True)
    except Exception as exc:
        raise ChannelImportError(f"File tidak dapat dibaca sebagai Excel: {exc}") from exc

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ChannelImportError("File Excel kosong.")

    index = _build_col_index(rows[0])

    # Row 1 (index 0) = column names, row 2 (index 1) = description — skip both.
    data_rows = rows[2:] if len(rows) > 2 else []
    if not data_rows:
        raise ChannelImportError("File Excel kosong (tidak ada data pesanan).")

    grouped: OrderedDict = OrderedDict()
    skipped_ids: set = set()

    for row_num, row in enumerate(data_rows, start=3):
        order_id = _get(row, index, "order_id")
        if not order_id:
            continue

        status = _get(row, index, "status")
        if status in _SKIP_STATUSES:
            skipped_ids.add(order_id)
            continue

        try:
            raw_qty = _get(row, index, "quantity") or "0"
            quantity = int(parse_amount(raw_qty))
            if quantity <= 0:
                raise ChannelImportError("jumlah harus lebih dari 0")
            unit_price = parse_amount(_get(row, index, "unit_price"))
        except ChannelImportError as exc:
            raise ChannelImportError(f"Baris {row_num}: {exc}") from exc

        line = OrderLine(
            sku=_sku(row, index),
            quantity=quantity,
            unit_price=unit_price,
            name=_get(row, index, "product_name"),
        )

        head = grouped.setdefault(order_id, {
            "row": row,
            "lines": [],
            "status": status,
            "seller_discount": Decimal("0"),
        })
        head["lines"].append(line)

        # Accumulate per-line seller discounts into order-level total
        raw_discount = _get(row, index, "seller_discount") or "0"
        try:
            head["seller_discount"] += parse_amount(raw_discount)
        except Exception:
            pass

    skipped = len(skipped_ids - set(grouped))

    orders = []
    for order_id, head in grouped.items():
        row = head["row"]
        orders.append(MarketplaceOrder(
            external_id=order_id[:80],
            buyer_name=_buyer_name(row, index),
            phone=_phone(row, index),
            address=_address(row, index),
            lines=head["lines"],
            shipping_cost=parse_amount(_get(row, index, "shipping_fee")),
            discount=head["seller_discount"],
            courier=_get(row, index, "courier"),
            tracking_number=_get(row, index, "tracking"),
            raw={
                "tokopedia_status": head["status"],
                "tokopedia_username": _get(row, index, "buyer_username"),
                "city": _get(row, index, "city"),
                "province": _get(row, index, "province"),
            },
        ))

    return orders, skipped
