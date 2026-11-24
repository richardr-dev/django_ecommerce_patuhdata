"""Rows pasted from a spreadsheet onto the Saldo awal pages (receivables per invoice, payables per bill).

Five columns per line: ``party; number; date; due date; amount``, separated by tabs (a copy from Excel)
or semicolons (typed). A header row is skipped. Each row comes back with an ``error`` explaining what
could not be read, so the caller can refuse the whole paste naming the first bad line."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

HEADER_WORDS = {"pelanggan", "customer", "nama", "supplier", "pemasok", "vendor"}


@dataclass
class Row:
    line: int
    party: str
    number: str
    issued_on: date | None
    due_date: date | None
    amount: Decimal | None
    error: str = ""


def parse_date(text):
    text = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(text):
    """``1.250.000``, ``1.250.000,00``, ``Rp 750000`` → Decimal; None when unreadable."""
    text = (text or "").strip().replace("Rp", "").replace(" ", "")
    if "," in text and text.rsplit(",", 1)[1].isdigit() and len(text.rsplit(",", 1)[1]) <= 2:
        text = text.rsplit(",", 1)[0]
    text = text.replace(".", "").replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse(text, *, party="pelanggan", document="invoice"):
    """``party`` and ``document`` only word the error messages ("nama supplier dan nomor tagihan wajib")."""
    rows = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        cells = [c.strip() for c in (raw.split("\t") if "\t" in raw else raw.split(";"))]
        if number == 1 and cells and cells[0].lower() in HEADER_WORDS:
            continue
        cells += [""] * (5 - len(cells))
        name, ref, issued, due, amount = cells[:5]
        row = Row(number, name, ref, parse_date(issued), parse_date(due) if due else None, parse_amount(amount))
        if not name or not ref:
            row.error = f"nama {party} dan nomor {document} wajib"
        elif row.issued_on is None:
            row.error = "tanggal tidak terbaca (pakai 31/12/2025)"
        elif due and row.due_date is None:
            row.error = "jatuh tempo tidak terbaca"
        elif row.amount is None or row.amount <= 0:
            row.error = "sisa tagihan tidak terbaca"
        rows.append(row)
    return rows


def duplicates(rows):
    numbers = [row.number for row in rows]
    return sorted({n for n in numbers if numbers.count(n) > 1})
