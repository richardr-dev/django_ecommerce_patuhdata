"""Read an uploaded CSV or Excel sheet as a list of rows keyed by lower-case header (used by the data
migration imports: products and customers)."""
import csv
import io

MAX_BYTES = 10 * 1024 * 1024


def _cell(value):
    """An Excel cell as text: whole numbers without ``.0``, dates as 31/12/2026."""
    import datetime
    if value is None:
        return ""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).strip()


def read_rows(upload, *, max_rows=10000):
    """Rows of ``upload`` (.csv or .xlsx) as dicts. Raises ValueError with a message for the screen."""
    if upload.size > MAX_BYTES:
        raise ValueError("Ukuran file maksimal 10 MB.")
    name = upload.name.lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        try:
            sheet = load_workbook(io.BytesIO(upload.read()), read_only=True, data_only=True).worksheets[0]
        except Exception:  # noqa: BLE001 - any unreadable workbook is the same message
            raise ValueError("File Excel tidak dapat dibaca.") from None
        table = [[_cell(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    elif name.endswith(".csv"):
        text = upload.read().decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        table = list(csv.reader(io.StringIO(text), dialect))
    else:
        raise ValueError("Gunakan file .csv atau .xlsx.")
    table = [row for row in table if any(str(cell).strip() for cell in row)]
    if not table:
        raise ValueError("File kosong.")
    headers = [str(h).strip().lower() for h in table[0]]
    rows = [{headers[i]: str(value).strip() for i, value in enumerate(row) if i < len(headers) and headers[i]}
            for row in table[1:]]
    if not rows:
        raise ValueError("File tidak berisi data di bawah judul kolom.")
    if len(rows) > max_rows:
        raise ValueError(f"Maksimal {max_rows:,} baris per impor.".replace(",", "."))
    return headers, rows
