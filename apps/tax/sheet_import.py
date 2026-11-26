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


def _number(text):
    """A sheet amount as a float: "1,198,000", "1.198.000" or 1198000.0. None when unreadable."""
    text = _amount(text)
    if not text:
        return None
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", text):          # thousands separators only
        text = re.sub(r"[.,]", "", text)
    else:
        text = text.replace(",", ".") if text.count(",") == 1 and "." not in text else text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _paste_number(value):
    """A number as the paste reads it: whole rupiah plain, cents after a comma (a dot is a thousands mark)."""
    value = round(value, 2)
    return str(int(value)) if value == int(value) else f"{value:.2f}".replace(".", ",")


# Indonesian headers of the sales sheet → the English ones the reader works with (both are accepted).
SALES_ALIASES = {
    "date": ("tanggal", "tgl", "tanggal penjualan"),
    "sku": ("kode barang", "kode produk", "sku produk"),
    "qty": ("jumlah", "kuantitas", "jumlah barang"),
    "customer name": ("nama pelanggan", "nama pembeli", "pembeli"),
    "address": ("alamat", "alamat kirim", "alamat pengiriman"),
    "phone number": ("no. hp", "no hp", "nomor hp", "no. whatsapp", "whatsapp", "telepon", "no. telepon"),
    "district": ("kecamatan", "kota", "kabupaten/kota", "kota/kabupaten", "kecamatan/kota"),
    "province": ("provinsi",),
    "sales channel": ("saluran penjualan", "saluran", "channel"),
    "sales": ("penjualan", "total penjualan", "harga jual total"),
    "net sales": ("penjualan bersih", "diterima", "net"),
    "unit price": ("harga satuan",),
    "due date": ("jatuh tempo",),
}


def _sales_headers(headers, rows):
    """Rename Indonesian headers to the English names; a header already in English is left as it is."""
    rename = {}
    for english, names in SALES_ALIASES.items():
        if english in headers:
            continue
        found = next((name for name in names if name in headers), None)
        if found:
            rename[found] = english
    if not rename:
        return headers, rows
    return ([rename.get(h, h) for h in headers],
            [{rename.get(key, key): value for key, value in row.items()} for row in rows])


def _customer_names(store):
    return {c.customer_code.lower(): c.name for c in Customer.objects.filter(store=store, customer_code__gt="")}


def invoice_rows(store, upload):
    """``(text, notes)``: paste rows for Buat banyak invoice.

    Three layouts, told apart by their headers:
    - **Accurate** ``sales-invoice-import-file``: CUSTOMER NO, NUMBER, DATE, ITEM:ITEM NO, ITEM:QUANTITY... A row
      without customer number continues the invoice above it. Customer numbers must already be in Pelanggan.
      A row with no item but an EXPENSE:EXPENSE VALUE (Accurate's "biaya lain") becomes the invoice's ongkir
      (4200, outside PPN); a negative one is left out and counted.
    - **Sales sheet** (our template: Date, SKU, Qty, Customer Name, Address, Phone Number, District, Province,
      Sales Channel, Status, Sales, Net Sales, Ongkir, optional No. Pesanan / Ref): one invoice per row, or per Ref. ``Sales`` is
      the line total at the retail price, so the price is Sales / Qty; ``Net Sales`` (what the company keeps
      after the mitra or marketplace cut) makes the rest a line discount, so the invoice shows both. ``Ongkir``
      is the courier cost the company paid (an expense, not billed). A Sales Channel naming a mitra puts the
      sale under that mitra. A Retur row is invoiced and credited back
      (``status`` column); rows with any other Status than Sales are left out and counted."""
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
                    out.append([number, name, date, due, "", "", "", "", "", "", "", fee, "", ""])
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
                        "", "", "", "", "", ""])
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
        headers, rows = _sales_headers(headers, rows)
        need = {"sku", "qty"}
        if not need.issubset(headers) or not ({"customer name", "pelanggan"} & set(headers)):
            raise ValueError("Kolom wajib: Tanggal, SKU, Jumlah, Nama Pelanggan (atau Date, SKU, Qty, Customer Name). "
                             "Gunakan template yang bisa diunduh di halaman ini.")
        left, cut, returned = 0, 0, 0
        for row in rows:
            status = row.get("status", "").lower()
            is_return = any(word in status for word in ("retur", "return", "dikembalikan", "refund"))
            if status and not is_return and status not in {"sales", "penjualan", "selesai", "lunas"}:
                left += 1
                continue
            returned += is_return
            qty = row.get("qty", "") or "1"
            price = _first(row, "unit price", "harga satuan", "harga")
            if not price and row.get("sales"):
                total, count = _number(row["sales"]), _number(qty)
                price = _paste_number(total / count) if total is not None and count else row["sales"]
            discount = _first(row, "discount", "diskon")
            net = row.get("net sales", "")
            if not discount and net and row.get("sales"):
                gross_total, net_total = _number(row["sales"]), _number(net)
                if gross_total is not None and net_total is not None and 0 <= net_total < gross_total:
                    discount = _paste_number(gross_total - net_total)
                    cut += 1
            address = row.get("address", "")
            place = ", ".join([address] + [x for x in (row.get("district", ""), row.get("province", ""))
                                           if x and x.lower() not in address.lower()]).strip(", ")
            out.append([_first(row, "no. pesanan", "no pesanan", "nomor pesanan", "order id", "ref", "no. ref",
                                   "referensi"), _first(row, "customer name", "pelanggan"),
                        _first(row, "date", "tanggal"), _first(row, "due date", "jatuh tempo"),
                        _first(row, "sku", "barang"), qty, price, discount,
                        _first(row, "phone number", "telepon", "phone"), place,
                        _first(row, "sales channel", "saluran"), _first(row, "ongkir ditagih"),
                        _first(row, "ongkir", "ongkos kirim", "ongkir dibayar"), "retur" if is_return else ""])
        if left:
            notes.append(f"{left} baris dengan Status selain Sales atau Retur (mis. Batal) dilewati.")
        if returned:
            notes.append(f"{returned} baris Retur: penjualannya dibuat lalu langsung dibuatkan nota kredit (barang "
                         "kembali ke stok, ongkirnya tetap dicatat sebagai biaya).")
        if cut:
            notes.append(f"{cut} baris: selisih Penjualan dan Penjualan Bersih dicatat sebagai diskon baris, "
                         "jadi invoice bernilai Penjualan Bersih dan harga jualnya tetap terlihat.")
        if any(row.get("ongkir") or row.get("ongkos kirim") for row in rows):
            notes.append("Ongkir dicatat sebagai biaya ongkos kirim yang dibayar perusahaan (akun 6700), tidak "
                         "ditagihkan ke pembeli. Pilih rekening pembayarnya sebelum membuat invoice.")
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
