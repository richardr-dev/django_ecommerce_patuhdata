"""Read a marketplace income / settlement report (Shopee "Penghasilan", TikTok Shop and
Tokopedia "Settlement") into one row per order.

The marketplaces change their column names between exports, so columns are recognised by
keywords in their headers, in Indonesian or English, instead of by exact names. Only the order
number and the amount released are required. Whatever the marketplace deducted beyond the fees
it itemises is kept as "other", so a row always reconciles to what was paid out."""
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import openpyxl

from apps.channels.services import ChannelImportError, parse_amount

ORDER_WORDS = ("no. pesanan", "nomor pesanan", "order id", "order/adjustment id", "order no")
NET_WORDS = ("total penghasilan", "jumlah dana dilepas", "dana dilepas", "total settlement",
    "settlement amount", "net amount", "total payout", "penghasilan")
DATE_WORDS = ("tanggal dana dilepaskan", "dana dilepaskan", "release date", "settlement time",
    "settled time", "tanggal pencairan", "tanggal settlement", "payout date")
# Header keywords -> fee category. Checked in order, first match wins.
FEE_WORDS = [
    ("affiliate", ("afiliasi", "affiliate", "ams")),
    ("voucher", ("voucher", "diskon ditanggung penjual", "seller discount", "seller coupon")),
    ("commission", ("komisi", "commission")),
    ("service_fee", ("biaya layanan", "service fee")),
    ("admin_fee", ("administrasi", "transaction fee", "payment fee", "biaya transaksi",
        "biaya proses")),
    ("shipping_fee", ("potongan biaya pengiriman", "shipping fee charged", "seller shipping",
        "ongkir ditanggung", "selisih ongkir", "shipping fee difference", "return shipping",
        "ongkos kirim pengembalian")),
]
NOT_A_FEE = ("harga", "price", "subtotal", "total pembayaran", "refund", "pengembalian dana")
SKIPPABLE = {"", "none"}


@dataclass
class IncomeRow:
    order_id: str
    net: Decimal
    released_on: date | None
    fees: dict
    line: int


def _norm(value):
    return str(value or "").strip().lower()


def _find(headers, words):
    for word in words:  # earlier words are more specific
        for index, header in enumerate(headers):
            if word in header:
                return index
    return None


def _classify(headers, used):
    """{column index: fee category} for every itemised fee column."""
    columns = {}
    for index, header in enumerate(headers):
        if index in used or not header or any(word in header for word in NOT_A_FEE):
            continue
        for category, words in FEE_WORDS:
            if any(word in header for word in words):
                columns[index] = category
                break
    return columns


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    for layout in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, layout).date()
        except ValueError:
            continue
    return None


def _amount(value):
    """A cell as Decimal: numbers, "15.000", "Rp 15.000", "-15.000" and "(15.000)"."""
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value or "").strip()
    if text in {"", "-"}:
        return Decimal(0)
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    amount = parse_amount(text.strip("-()"))
    return -amount if negative else amount


def _rows(upload, name):
    if name.lower().endswith(".csv"):
        text = upload.read().decode("utf-8-sig") if hasattr(upload, "read") else upload
        return [row for row in csv.reader(io.StringIO(text))]
    try:
        workbook = openpyxl.load_workbook(upload, data_only=True)
    except Exception as error:
        raise ChannelImportError(f"File tidak dapat dibaca sebagai Excel: {error}") from error
    return [list(row) for row in workbook.active.iter_rows(values_only=True)]


def parse_income(upload, name="income.xlsx"):
    """Rows of an income report. Raises ChannelImportError when the columns are not found."""
    rows = _rows(upload, name)
    for start, row in enumerate(rows[:10]):
        headers = [_norm(cell) for cell in row]
        order_col = _find(headers, ORDER_WORDS)
        if order_col is not None and _find(headers, NET_WORDS) is not None:
            break
    else:
        raise ChannelImportError("Kolom nomor pesanan dan jumlah dana yang dicairkan tidak "
            "ditemukan. Pastikan ini laporan penghasilan / settlement dari marketplace.")
    net_col = _find(headers, NET_WORDS)
    date_col = _find(headers, DATE_WORDS)
    fee_cols = _classify(headers, {order_col, net_col, date_col})
    result = []
    for number, row in enumerate(rows[start + 1:], start=start + 2):
        order_id = str(row[order_col] if order_col < len(row) else "").strip()
        if _norm(order_id) in SKIPPABLE or order_id.lower().startswith("total"):
            continue
        try:
            net = _amount(row[net_col] if net_col < len(row) else 0)
            fees = {}
            for index, category in fee_cols.items():
                value = row[index] if index < len(row) else 0
                if _norm(value) in SKIPPABLE:
                    continue
                fees[category] = fees.get(category, Decimal(0)) + abs(_amount(value))
        except ChannelImportError as error:
            raise ChannelImportError(f"Baris {number}: {error}") from error
        released = _date(row[date_col]) if date_col is not None and date_col < len(row) else None
        result.append(IncomeRow(order_id=order_id[:80], net=net, released_on=released,
            fees=fees, line=number))
    if not result:
        raise ChannelImportError("Tidak ada baris pesanan di file ini.")
    return result
