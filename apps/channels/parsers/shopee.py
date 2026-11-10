"""
Parse Shopee Seller Center XLSX exports into MarketplaceOrder objects.

Shopee's native export masks phone numbers, partial names, and partial addresses.
We use Username (Pembeli) as the buyer identifier since it is always unmasked.
Cancelled orders (Status Pesanan = Batal) are silently skipped.
"""
from collections import OrderedDict

import openpyxl

from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.services import ChannelImportError, parse_amount

# Map internal keys → Shopee column header text.
# Shopee may add or reorder columns between exports; we match by name, not position.
_COLS = {
    "order_id":        "No. Pesanan",
    "status":          "Status Pesanan",
    "tracking":        "No. Resi",
    "courier":         "Opsi Pengiriman",
    "sku_parent":      "SKU Induk",
    "product_name":    "Nama Produk",
    "sku_ref":         "Nomor Referensi SKU",
    "variation":       "Nama Variasi",
    "unit_price":      "Harga Setelah Diskon",
    "quantity":        "Jumlah",
    "seller_discount": "Diskon Dari Penjual",
    "shipping_cost":   "Ongkos Kirim Dibayar oleh Pembeli",
    "total":           "Total Pembayaran",
    "username":        "Username (Pembeli)",
    "recipient_name":  "Nama Penerima",
    "phone_masked":    "No. Telepon",
    "address":         "Alamat Pengiriman",
    "city":            "Kota/Kabupaten",
    "province":        "Provinsi",
}

# These statuses are never imported — Shopee shows them in all-order exports.
_SKIP_STATUSES = {"Batal", "Dibatalkan"}

_REQUIRED_KEYS = {"order_id", "product_name", "unit_price", "quantity"}


def _build_col_index(header_row):
    """Return {internal_key: col_index} resolved from the header row."""
    name_to_idx = {str(v or "").strip(): i for i, v in enumerate(header_row)}
    index = {key: name_to_idx[col] for key, col in _COLS.items() if col in name_to_idx}
    missing = _REQUIRED_KEYS - set(index)
    if missing:
        col_names = ", ".join(_COLS[k] for k in missing)
        raise ChannelImportError(
            f"File bukan format ekspor Shopee. Kolom wajib tidak ditemukan: {col_names}"
        )
    return index


def _get(row, index, key, default=""):
    idx = index.get(key)
    return str(row[idx] or "").strip() if idx is not None else default


def _extract_courier(courier_str):
    """'Reguler (Cashless)-Ninja Xpress' → 'Ninja Xpress'"""
    return courier_str.rsplit("-", 1)[-1].strip() if courier_str else ""


def _sku(row, index):
    """Best available SKU: Nomor Referensi SKU → SKU Induk → descriptive placeholder.

    When neither is set the seller hasn't configured Nomor Referensi SKU in Shopee.
    The placeholder produces a clear error from _resolve_sku so the seller knows what to fix.
    """
    for key in ("sku_ref", "sku_parent"):
        val = _get(row, index, key)
        if val:
            return val
    name = _get(row, index, "product_name")
    variation = _get(row, index, "variation")
    display = f"{name} {variation}".strip()[:50]
    return f"[Tanpa SKU: {display}]"


def _buyer_name(row, index):
    name = _get(row, index, "recipient_name")
    # Shopee masks names with asterisks; fall back to username when masked.
    if name and "*" not in name:
        return name
    username = _get(row, index, "username")
    return f"@{username}" if username else "Pembeli Shopee"


def _phone(row, index):
    # Shopee always masks the phone number. The username is unique and always visible.
    return _get(row, index, "username") or "-"


def _address(row, index):
    parts = filter(None, [
        _get(row, index, "address"),
        _get(row, index, "city"),
        _get(row, index, "province"),
    ])
    return ", ".join(parts) or "-"


def parse_shopee_xlsx(upload):
    """Parse a Shopee Seller Center XLSX export.

    Returns a list of MarketplaceOrder objects ready for import_orders().
    Also returns skipped_count (cancelled orders).

    upload may be a Django InMemoryUploadedFile, TemporaryUploadedFile, or file path.
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

    if len(rows) < 2:
        raise ChannelImportError("File Excel kosong (tidak ada data pesanan).")

    grouped = OrderedDict()
    skipped_ids: set[str] = set()

    for row_num, row in enumerate(rows[1:], start=2):
        order_id = _get(row, index, "order_id")
        if not order_id:
            continue

        status = _get(row, index, "status")
        if status in _SKIP_STATUSES:
            # Count unique cancelled order IDs, not rows, so multi-line orders aren't inflated.
            skipped_ids.add(order_id)
            continue

        try:
            quantity = int(parse_amount(_get(row, index, "quantity") or "0"))
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
        head = grouped.setdefault(order_id, {"row": row, "lines": [], "status": status})
        head["lines"].append(line)

    # Cancelled orders that also appear as completed (re-ordered) shouldn't be counted twice.
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
            shipping_cost=parse_amount(_get(row, index, "shipping_cost")),
            discount=parse_amount(_get(row, index, "seller_discount")),
            courier=_extract_courier(_get(row, index, "courier")),
            tracking_number=_get(row, index, "tracking"),
            raw={
                "shopee_status": head["status"],
                "shopee_username": _get(row, index, "username"),
                "city": _get(row, index, "city"),
                "province": _get(row, index, "province"),
            },
        ))

    return orders, skipped
