"""Bring customers over from another system (Accurate, Excel...): one row per customer.

Columns of the template: nomor_pelanggan, nama, perusahaan, telepon, email, alamat, npwp,
termin_hari, batas_kredit, tag. ``nomor_pelanggan`` is the customer's number in the old system;
left empty, the system makes a unique one (PLG-00001...). A customer already in the books (same
number, else same phone) is updated, never doubled. All rows are checked first; one bad row stops
the whole import, so nothing is half done."""
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.tax.services import normalize_npwp

from .models import Customer

COLUMNS = ["nomor_pelanggan", "nama", "perusahaan", "telepon", "email", "alamat", "npwp",
           "termin_hari", "batas_kredit", "tag"]
SAMPLE = [
    ["C-0001", "Budi Santoso", "", "081234567890", "budi@contoh.com", "Jl. Merdeka 10, Bandung", "", "", "", "pelanggan"],
    ["C-0002", "Toko Maju Jaya", "PT Maju Jaya", "021555123", "kantor@majujaya.co.id", "Jl. Sudirman 5, Jakarta",
     "0123456789012345", "30", "5000000", "reseller, vip"],
]


def _amount(text):
    text = (text or "").replace("Rp", "").replace(" ", "")
    if not text:
        return None
    if "," in text:                      # 1.250.000,00
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", text):   # 1.250.000
        text = text.replace(".", "")
    return Decimal(text)


# The same file may come straight from Accurate ("Template Impor Pelanggan"): its headers are read too.
ACCURATE = {"id pelanggan": "nomor_pelanggan", "handphone": "telepon", "no. telp. bisnis": "_telp_bisnis",
            "nomor wajib pajak": "npwp", "jumlah limit piutang": "batas_kredit", "syarat pembayaran": "termin_hari",
            "alamat penagihan": "alamat", "kategori": "_kategori", "kota": "_kota", "provinsi": "_provinsi"}


def from_accurate(row):
    """Our columns from a row that may use Accurate's headers; ours win when both exist."""
    row = dict(row)
    for old, new in ACCURATE.items():
        if row.get(old) and not row.get(new):
            row[new] = row[old]
    row.setdefault("nama", "")
    if not row.get("telepon") and row.get("_telp_bisnis"):
        row["telepon"] = row["_telp_bisnis"]
    if row.get("_kota") or row.get("_provinsi"):
        row["alamat"] = ", ".join(x for x in (row.get("alamat"), row.get("_kota"), row.get("_provinsi")) if x)
    if row.get("_kategori") and row["_kategori"].lower() != "umum" and not row.get("tag"):
        row["tag"] = row["_kategori"]
    if row.get("termin_hari") and not re.search(r"\d", row["termin_hari"]):
        row["termin_hari"] = ""          # "C.O.D" and the like carry no number of days
    return row


def parse(rows):
    parsed, seen_code, seen_phone = [], {}, {}
    for number, row in enumerate((from_accurate(r) for r in rows), 2):
        name = row.get("nama", "").strip()[:120]
        if not name:
            raise ValueError(f"Baris {number}: nama wajib diisi.")
        code = row.get("nomor_pelanggan", "").strip()[:80]
        phone = re.sub(r"[^\d+]", "", row.get("telepon", ""))[:30]
        for key, seen, label in ((code, seen_code, "Nomor pelanggan"), (phone, seen_phone, "Telepon")):
            if key:
                if key in seen:
                    raise ValueError(f"Baris {number}: {label} {key} sudah muncul di baris {seen[key]}.")
                seen[key] = number
        try:
            npwp = normalize_npwp(row.get("npwp", ""))
        except ValueError as error:
            raise ValueError(f"Baris {number}: {error}") from None
        days = row.get("termin_hari", "").strip()
        days = re.sub(r"\D", "", days)
        try:
            limit = _amount(row.get("batas_kredit", ""))
        except InvalidOperation:
            raise ValueError(f"Baris {number}: batas_kredit harus berupa angka.") from None
        if limit is not None and (limit < 0 or limit >= 10 ** 13):
            raise ValueError(f"Baris {number}: batas_kredit tidak valid.")
        if days and int(days) > 365:
            raise ValueError(f"Baris {number}: termin_hari maksimal 365.")
        parsed.append({"number": number, "code": code, "phone": phone, "name": name,
            "company_name": row.get("perusahaan", "").strip()[:160], "email": row.get("email", "").strip()[:254],
            "address": row.get("alamat", "").strip(), "npwp": npwp,
            "payment_terms_days": int(days) if days else None, "credit_limit": limit,
            "tags": row.get("tag", "").strip()[:300]})
    return parsed


def _next_code(store):
    used = set(Customer.objects.filter(store=store, customer_code__startswith="PLG-")
               .values_list("customer_code", flat=True))
    highest = max([int(c[4:]) for c in used if c[4:].isdigit()] or [0])
    while True:
        highest += 1
        yield f"PLG-{highest:05d}"


@transaction.atomic
def import_customers(store, parsed):
    """Create or update customers. Returns ``(created, updated)``."""
    by_code = {c.customer_code: c for c in Customer.objects.filter(store=store, customer_code__gt="")}
    by_phone = {c.phone: c for c in Customer.objects.filter(store=store, phone__gt="")}
    codes, created, updated = _next_code(store), 0, 0
    for row in parsed:
        customer = by_code.get(row["code"]) if row["code"] else None
        customer = customer or (by_phone.get(row["phone"]) if row["phone"] else None)
        fields = {"name": row["name"], "company_name": row["company_name"], "address": row["address"],
                  "npwp": row["npwp"], "payment_terms_days": row["payment_terms_days"],
                  "credit_limit": row["credit_limit"], "tags": row["tags"]}
        if row["email"]:
            fields["email"] = row["email"]
        if customer is None:
            code = row["code"] or next(codes)
            customer = Customer(store=store, customer_code=code, phone=row["phone"],
                                status=Customer.STATUS_ACTIVE, **{"email": "", **fields})
            customer.save()
            created += 1
        else:
            for key, value in fields.items():
                if value not in ("", None):   # an empty cell keeps what is already saved
                    setattr(customer, key, value)
            if row["phone"] and not customer.phone and row["phone"] not in by_phone:
                customer.phone = row["phone"]
            if row["code"] and not customer.customer_code:
                customer.customer_code = row["code"]
            customer.save()
            updated += 1
        if customer.customer_code:
            by_code[customer.customer_code] = customer
        if customer.phone:
            by_phone[customer.phone] = customer
    return created, updated
