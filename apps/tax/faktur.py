"""Faktur Pajak Keluaran worksheet: the invoices and credit notes of a month as one row per line,
checked for what the tax office will reject, ready to copy into the DJP Coretax template.

This is NOT the Coretax XML. The official import is an Excel template converted to XML by DJP's
converter (versions change: the template and the converter have been on different versions), and the
field layout is not confirmed here, so the worksheet carries the data and the problems, and the
person fills the current template from it."""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from apps.accounting.exports import Sheet
from apps.tax.models import CreditNote, Invoice

ZERO = Decimal(0)
LIMITATION = ("Lembar ini membantu mengisi template Faktur Pajak Keluaran dari DJP; ini bukan file XML "
    "Coretax. Unduh template dan converter terbaru dari pajak.go.id (bagian Coretax, Template XML dan "
    "Converter Excel ke XML), isi dari lembar ini, konversi, lalu impor di Coretax. Nomor Faktur Pajak "
    "resmi diterbitkan Coretax, bukan aplikasi ini.")


def _digits(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


def buyer_problem(npwp):
    """None when the buyer id looks usable: NPWP 15/16 digits or an NIK of 16 digits."""
    digits = _digits(npwp)
    if not digits:
        return "NPWP/NIK pembeli kosong"
    if len(digits) not in (15, 16):
        return "NPWP/NIK pembeli bukan 15 atau 16 digit"
    return None


def _spread(total, parts):
    """Split ``total`` over ``parts`` weights to whole rupiah, the last takes the remainder."""
    weight = sum(parts, ZERO)
    out, used = [], ZERO
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            out.append(total - used)
        else:
            share = (total * part / weight).quantize(Decimal(1), ROUND_HALF_UP) if weight else ZERO
            out.append(share)
            used += share
    return out


def month_range(year, month):
    first = datetime.date(year, month, 1)
    return first, (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)


def rows(store, year, month):
    """``{"invoices": [...], "notes": [...], "problems": [...]}``; each row is a dict per line."""
    first, last = month_range(year, month)
    invoices = Invoice.objects.filter(store=store, issued_on__gte=first, issued_on__lte=last, tax__gt=0
        ).exclude(status__in=[Invoice.STATUS_VOID, Invoice.STATUS_DRAFT]).prefetch_related("items").order_by(
        "issued_on", "number")
    notes = CreditNote.objects.filter(store=store, issued_on__gte=first, issued_on__lte=last, tax__gt=0,
        status=CreditNote.STATUS_ISSUED).select_related("invoice").prefetch_related("items").order_by(
        "issued_on", "number")
    out = {"invoices": [], "notes": [], "problems": []}
    for invoice in invoices:
        items = list(invoice.items.all())
        issue = buyer_problem(invoice.buyer_npwp)
        if issue:
            out["problems"].append((invoice.number, issue))
        if not items:
            out["problems"].append((invoice.number, "Invoice tanpa baris barang/jasa"))
            continue
        dpp = _spread(invoice.tax_base, [item.line_total for item in items])
        ppn = _spread(invoice.tax, [item.line_total for item in items])
        for number, (item, d, v) in enumerate(zip(items, dpp, ppn, strict=True), 1):
            out["invoices"].append({"number": invoice.number, "date": invoice.issued_on, "line": number,
                "buyer": invoice.buyer_name, "npwp": _digits(invoice.buyer_npwp), "address": invoice.buyer_address,
                "code": item.sku, "name": item.description, "unit": item.unit, "qty": item.quantity,
                "price": item.unit_price, "discount": item.discount, "dpp": d, "rate": invoice.tax_rate, "ppn": v,
                "issue": issue or ""})
    for note in notes:
        items = list(note.items.all())
        dpp = _spread(note.subtotal, [item.line_total for item in items]) if items else []
        ppn = _spread(note.tax, [item.line_total for item in items]) if items else []
        if not items:
            out["problems"].append((note.number, "Nota retur tanpa baris"))
        for number, (item, d, v) in enumerate(zip(items, dpp, ppn, strict=True), 1):
            out["notes"].append({"number": note.number, "date": note.issued_on, "line": number,
                "invoice": note.invoice.number, "buyer": note.buyer_name, "npwp": _digits(note.buyer_npwp),
                "code": item.sku, "name": item.description, "qty": item.quantity, "price": item.unit_price,
                "dpp": d, "ppn": v})
    return out


def workbook(store, year, month):
    from openpyxl import Workbook
    data = rows(store, year, month)
    wb = Workbook()
    notes_sheet = Sheet(wb, "Catatan", store, f"Faktur Pajak Keluaran {month:02d}/{year}", first=True)
    for sentence in LIMITATION.split(". "):
        notes_sheet.line(sentence.strip().rstrip(".") + ".")
    notes_sheet.blank()
    notes_sheet.header("Dokumen", "Masalah yang perlu diperbaiki sebelum impor")
    for number, issue in data["problems"]:
        notes_sheet.add(number, issue)
    if not data["problems"]:
        notes_sheet.add("—", "Tidak ada masalah ditemukan.")
    notes_sheet.finish()

    sheet = Sheet(wb, "Faktur keluaran", store, f"{month:02d}/{year}")
    sheet.header("No. invoice", "Tanggal", "Baris", "Pembeli", "NPWP/NIK pembeli", "Alamat pembeli", "Kode",
        "Nama barang/jasa", "Satuan", "Qty", "Harga satuan", "Diskon", "DPP", "Tarif PPN %", "PPN", "Periksa")
    for r in data["invoices"]:
        sheet.add(r["number"], r["date"], r["line"], r["buyer"], r["npwp"], r["address"], r["code"], r["name"],
            r["unit"], r["qty"], r["price"], r["discount"], r["dpp"], r["rate"], r["ppn"], r["issue"])
    sheet.finish("A5")

    retur = Sheet(wb, "Nota retur", store, f"{month:02d}/{year}")
    retur.header("No. nota", "Tanggal", "Baris", "Atas invoice", "Pembeli", "NPWP/NIK pembeli", "Kode",
        "Nama barang/jasa", "Qty", "Harga satuan", "DPP", "PPN")
    for r in data["notes"]:
        retur.add(r["number"], r["date"], r["line"], r["invoice"], r["buyer"], r["npwp"], r["code"], r["name"],
            r["qty"], r["price"], r["dpp"], r["ppn"])
    retur.finish("A5")
    return wb, data
