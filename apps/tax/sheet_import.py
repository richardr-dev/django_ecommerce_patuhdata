"""Turn an uploaded sheet (our template, a sales sheet kept in Excel, or Accurate's own import files) into
the rows the two bulk pages already understand: ``invoice_rows`` for Buat banyak invoice (tab separated, the
paste format) and ``receipt_rows`` for Impor penerimaan. Nothing is written here; the pages check the rows and
show what is wrong before anything is saved."""
import re

from apps.shops.models import Customer
from apps.shops.tabular import read_rows


def _clean(value):
    return re.sub(r"[\t\r\n]+", " ", value or "").strip()


def _first(row, *names):
    return next((row[n] for n in names if row.get(n)), "")


def _amount(text):
    """Digits of a sheet amount as a plain number text (the cells were already cleaned by the reader)."""
    text = (text or "").strip()
    return re.sub(r"[^\d,.-]", "", text)


def _customer_names(store):
    return {c.customer_code.lower(): c.name for c in Customer.objects.filter(store=store, customer_code__gt="")}


def invoice_rows(store, upload):
    """``(text, notes)``: paste rows for Buat banyak invoice.

    Three layouts, told apart by their headers:
    - **Accurate** ``sales-invoice-import-file``: CUSTOMER NO, NUMBER, DATE, ITEM:ITEM NO, ITEM:QUANTITY... A row
      without customer number continues the invoice above it. Customer numbers must already be in Pelanggan.
      A row with no item but an EXPENSE:EXPENSE VALUE (Accurate's "biaya lain") becomes the invoice's ongkir
      (4200, outside PPN); a negative one is left out and counted.
    - **Sales sheet** (Date, SKU, Qty, Customer Name, Address, Phone Number, District, Province, Sales Channel,
      Status, Sales, Net Sales, Ongkir): one invoice per row. ``Sales`` is the line total, so the price is
      Sales / Qty. Rows whose Status is not Sales are left out and counted.
      Its Ongkir is not invoiced (on a marketplace the buyer pays it to the marketplace).
    - Our template has the same columns plus an optional Ref and an Ongkir charged to the buyer; told from the
      sales sheet by having no Net Sales column."""
    headers, rows = read_rows(upload)
    notes, out = [], []
    if "item:item no" in headers:
        names, unknown, skipped, fees = _customer_names(store), [], 0, 0
        code = number = date = due = ""
        for index, row in enumerate(rows, 2):
            if row.get("customer no"):
                code, number = row["customer no"], row.get("number") or f"XLS-{index}"
                date, due = row.get("date", ""), row.get("due date", "")
            name = names.get(code.lower())
            if not name:
                if code not in unknown:
                    unknown.append(code)
                continue
            if not row.get("item:item no"):
                fee = _amount(row.get("expense:expense value", ""))
                try:
                    value = float(fee.replace(",", ".") or 0)
                except ValueError:
                    value = 0
                if value > 0:
                    fees += 1
                    out.append([number, name, date, due, "", "", "", "", "", "", "", fee])
                else:
                    skipped += 1
                continue
            qty, price = row.get("item:quantity", ""), row.get("item:unitprice", "")
            discount = row.get("item:item discount (rp)", "")
            percent = row.get("item:item discount (%)", "")
            if not discount and percent and price:
                try:
                    discount = str(round(float(qty or 1) * float(price) * float(percent.replace(",", ".")) / 100))
                except ValueError:
                    discount = ""
            out.append([number, name, date, due, row["item:item no"], qty, _amount(price), _amount(discount),
                        "", "", "", ""])
        if unknown:
            shown = ", ".join(unknown[:10]) + (" …" if len(unknown) > 10 else "")
            raise ValueError(f"Nomor pelanggan {shown} belum ada di Pelanggan. Impor pelanggan dulu "
                             "(Pelanggan › Impor pelanggan), lalu unggah file ini lagi.")
        if skipped:
            notes.append(f"{skipped} baris tanpa kode barang dan tanpa biaya positif dilewati.")
        if fees:
            notes.append(f"{fees} baris biaya lain (expense Accurate) dicatat sebagai ongkir invoice (akun 4200, "
                         "tanpa PPN). Periksa bila biaya itu bukan ongkos kirim.")
        if any(row.get("total including vat", "").lower() in {"yes", "ya"} for row in rows):
            notes.append("File menyebut total sudah termasuk PPN: centang “Harga sudah termasuk PPN” bila benar.")
    else:
        need = {"sku", "qty"}
        if not need.issubset(headers) or not ({"customer name", "pelanggan"} & set(headers)):
            raise ValueError("Kolom wajib: Date, SKU, Qty, Customer Name. Gunakan template yang bisa diunduh di "
                             "halaman ini.")
        left, own_template = 0, "net sales" not in headers
        for row in rows:
            status = row.get("status", "").lower()
            if status and status not in {"sales", "penjualan", "selesai", "lunas"}:
                left += 1
                continue
            qty = row.get("qty", "") or "1"
            price = _first(row, "unit price", "harga satuan", "harga")
            if not price and row.get("sales"):
                try:
                    total = float(_amount(row["sales"]).replace(".", "").replace(",", ".") or 0)
                    price = str(round(total / float(qty.replace(",", ".") or 1), 2)).rstrip("0").rstrip(".")
                except ValueError:
                    price = row["sales"]
            place = ", ".join(x for x in (row.get("address", ""), row.get("district", ""), row.get("province", ""))
                              if x)
            out.append([_first(row, "ref", "no. ref", "referensi"), _first(row, "customer name", "pelanggan"),
                        _first(row, "date", "tanggal"), _first(row, "due date", "jatuh tempo"),
                        _first(row, "sku", "barang"), qty, price, _first(row, "discount", "diskon"),
                        _first(row, "phone number", "telepon", "phone"), place,
                        _first(row, "sales channel", "saluran"),
                        _first(row, "ongkir", "ongkos kirim", "shipping") if own_template else ""])
        if left:
            notes.append(f"{left} baris dengan Status selain Sales dilewati.")
        if not own_template:
            notes.append("Ongkir dan Net Sales tidak dibuat menjadi invoice (Net Sales adalah hasil setelah "
                         "potongan marketplace).")
    if not out:
        raise ValueError("Tidak ada baris penjualan yang bisa dibaca di file ini.")
    return "\n".join("\t".join(_clean(cell) for cell in line) for line in out), notes


RECEIPT_COLUMNS = ["Tanggal", "Pelanggan", "No. penerimaan", "No. invoice", "Jumlah", "Metode", "Rekening", "Catatan"]


def receipt_rows(upload):
    """``(text, notes)``: rows for Impor penerimaan, every row complete (date, customer, method... repeated).

    Reads Accurate's ``sales-receipt-import-file`` (a row without customer number continues the receipt above:
    it only adds another invoice) and our template (Tanggal, Pelanggan, No. penerimaan, No. invoice, Jumlah,
    Metode, Rekening, Catatan)."""
    headers, rows = read_rows(upload)
    accurate = "payment number" in headers
    if not accurate and not {"no. invoice", "jumlah"}.issubset(headers):
        raise ValueError("Kolom wajib: Tanggal, Pelanggan, No. invoice, Jumlah. Gunakan template yang bisa diunduh "
                         "di halaman ini.")
    out, notes, discount, head = [], [], 0, {}
    for row in rows:
        if accurate:
            if row.get("customer no"):
                head = {"date": row.get("date", ""), "customer": row["customer no"], "number": row.get("number", ""),
                        "method": row.get("paying bank", ""), "bank": row.get("bank account no", ""),
                        "note": row.get("description", "")}
            invoice, value = row.get("payment number", ""), row.get("payment value", "")
            try:
                discount += int(float(_amount(row.get("total discount", "")).replace(",", ".") or 0))
            except ValueError:
                pass
            current = head
        else:
            if row.get("pelanggan"):
                head = {"date": row.get("tanggal", ""), "customer": row["pelanggan"],
                        "number": row.get("no. penerimaan", ""), "method": row.get("metode", ""),
                        "bank": row.get("rekening", ""), "note": row.get("catatan", "")}
            invoice, value = row.get("no. invoice", ""), row.get("jumlah", "")
            current = head
        if not invoice and not value:
            continue
        out.append([current.get("date", ""), current.get("customer", ""), current.get("number", ""), invoice,
                    _amount(value), current.get("method", ""), current.get("bank", ""), current.get("note", "")])
    if discount:
        notes.append("Kolom diskon pelunasan Accurate tidak dibukukan: hanya nilai pembayaran yang dicatat, jadi "
                     "invoice yang dilunasi dengan diskon masih punya sisa.")
    if not out:
        raise ValueError("Tidak ada baris pembayaran yang bisa dibaca di file ini.")
    return "\n".join("\t".join(_clean(cell) for cell in line) for line in out), notes
