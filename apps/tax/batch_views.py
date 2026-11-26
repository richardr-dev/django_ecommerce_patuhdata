"""Buat banyak invoice (``tax/invoice_batch.py``): paste rows from Excel, or bill the same lines to many
customers. "Periksa" shows what will be made; "Buat" plans again from what was typed (the server decides,
never the preview) and makes every invoice or none."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.accounting import idempotency
from apps.accounting.models import BankAccount
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog, Customer
from apps.shops.permissions import can, roles

from . import invoice_batch as batch
from . import sheet_import
from .invoice_form_views import _warehouse, _warehouses
from .services import InvoiceError, vat_for

SCOPE = "invoice_batch"
MODES = (("tempel", "Tempel dari Excel"), ("pelanggan", "Ke banyak pelanggan"))


def _plan(store, post, mode):
    inclusive = post.get("prices_include_tax") == "1"
    if mode == "tempel":
        return batch.parse_paste(store, post.get("rows", ""), inclusive=inclusive)
    ids = [value for value in post.getlist("customer[]") if value]
    customers = list(Customer.objects.filter(store=store, pk__in=ids).order_by("name")) if ids else []
    if not customers:
        raise InvoiceError("Centang minimal satu pelanggan.")
    count = len(post.getlist("item[]"))

    def column(name):
        values = post.getlist(name)
        return values + [""] * (count - len(values))
    items = list(zip(column("item[]"), column("qty[]"), column("price[]"), column("line_discount[]"), strict=False))
    if not any((text or "").strip() for text, *_rest in items):
        raise InvoiceError("Isi minimal satu baris barang atau jasa.")
    issued_on = parse_date(post.get("issued_on", "")) or timezone.localdate()
    return batch.for_customers(store, customers, items=items, issued_on=issued_on,
        due_date=parse_date(post.get("due_date", "")), reference=post.get("reference", "").strip(),
        inclusive=inclusive)


TEMPLATE_HEAD = ["Date", "SKU", "Qty", "Customer Name", "Address", "Phone Number", "District", "Province",
                 "Sales Channel", "Status", "Sales", "Net Sales", "Ongkir", "Ref"]
TEMPLATE_ROWS = [
    ["03/08/2026", "M2601-41-Black", 1, "Budi", "Jalan Pakan Dalam no.29, Kec. Daha Utara", "", "Hulu Sungai Selatan",
     "Kalimantan Selatan", "Shopee ABShop", "Sales", 1198000, 718800, None, None],
    ["04/08/2026", "W2601-38-White", 1, "Marulak Purba", "Jl. Cut Mutia No. 42, Kec. Teluk Betung Utara",
     "082115108880", "Bandar Lampung", "Lampung", "ABShop", "Sales", 998000, 598800, 26996, None],
    ["05/08/2026", "M2601-45-Black", 1, "H. Aidil Azwar", "Jl. Palem Raya no. 9, Jatiasih", "", "Bekasi",
     "Jawa Barat", "Shopee ABShop", "Sales", 1198000, 718800, None, None],
]
TEMPLATE_HELP = [
    ("Date", "Tanggal penjualan, mis. 03/08/2026 (hari/bulan/tahun). Kosong: hari ini."),
    ("SKU", "SKU produk atau SKU varian (ukuran/warna) persis seperti di Produk. Stok keluar dan HPP dibukukan."),
    ("Qty", "Jumlah barang. Kosong: 1."),
    ("Customer Name", "Nama pembeli. Nama baru ditambahkan ke Pelanggan."),
    ("Address, District, Province", "Alamat kirim pembeli; disimpan di pelanggan baru dan di pesanan."),
    ("Phone Number", "Nomor WhatsApp pembeli (opsional)."),
    ("Sales Channel", ("Tempat terjual. Bila memuat nama mitra (mis. Shopee ABShop), penjualan dicatat atas mitra itu; "
     "daftarkan mitranya dulu di Mitra. Shopee, Tokopedia, TikTok, Lazada dikenali sebagai saluran.")),
    ("Status", "Hanya baris Sales yang dibuat invoice; Retur dan lainnya dilewati."),
    ("Sales", "Total harga jual baris itu (harga satuan = Sales ÷ Qty)."),
    ("Net Sales", ("Yang diterima perusahaan setelah potongan mitra/marketplace. Selisihnya menjadi diskon baris, "
     "jadi invoice bernilai Net Sales. Kosong: tanpa diskon.")),
    ("Ongkir", ("Ongkos kirim yang dibayar perusahaan ke kurir. Dicatat sebagai biaya (6700) dari rekening yang "
     "dipilih di halaman impor; tidak ditagihkan ke pembeli.")),
    ("Ref", "Opsional. Baris dengan Ref yang sama menjadi satu invoice; tanpa Ref, satu baris satu invoice."),
]


@login_required(login_url="/login/")
def merchant_invoice_batch_template(request):
    """The Excel to fill in, laid out like a sales sheet kept by hand: one invoice per row, with a help sheet."""
    import io

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    book = Workbook()
    sheet = book.active
    sheet.title = "Penjualan"
    sheet.append(TEMPLATE_HEAD)
    for row in TEMPLATE_ROWS:
        sheet.append(row)
    head_fill = PatternFill("solid", fgColor="BFE8FB")
    for cell in sheet[1]:
        cell.font, cell.fill = Font(bold=True, color="012441"), head_fill
        cell.alignment = Alignment(horizontal="center")
    widths = [12, 18, 6, 22, 44, 16, 22, 20, 18, 10, 14, 14, 12, 10]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
    for row in sheet.iter_rows(min_row=2, max_row=500):
        for cell in row[10:13]:
            cell.number_format = "#,##0"
        row[0].number_format = "@"
    sheet.freeze_panes = "A2"
    guide = book.create_sheet("Petunjuk")
    guide.append(["Kolom", "Isi"])
    for row in TEMPLATE_HELP:
        guide.append(list(row))
    for cell in guide[1]:
        cell.font, cell.fill = Font(bold=True, color="012441"), head_fill
    guide.column_dimensions["A"].width, guide.column_dimensions["B"].width = 26, 110
    buffer = io.BytesIO()
    book.save(buffer)
    response = HttpResponse(buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="template-penjualan.xlsx"'
    return response


@login_required(login_url="/login/")
def merchant_invoice_batch(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    post = request.POST if request.method == "POST" else None
    mode = (post or request.GET).get("cara", "tempel")
    mode = mode if mode in dict(MODES) else "tempel"
    plans, error = None, ""
    if post is not None and post.get("action") == "upload":
        # A file becomes the pasted rows and is checked like any paste: nothing is written yet.
        post = post.copy()
        upload = request.FILES.get("file")
        try:
            if not upload:
                raise ValueError("Pilih file Excel atau CSV terlebih dahulu.")
            post["rows"], notes = sheet_import.invoice_rows(store, upload)
            post["action"], mode = "preview", "tempel"
            for note in notes:
                messages.info(request, note)
        except ValueError as exc:
            messages.error(request, str(exc))
            post = None
    may_override = can(request.user, store, "sales.credit")
    if post is not None:
        try:
            plans = _plan(store, post, mode)
            warehouse = _warehouse(store, post.get("warehouse"))
        except InvoiceError as exc:
            error = str(exc)
        if plans is not None and not error and post.get("action") == "create":
            idem = post.get("idem", "")
            earlier = idempotency.claim(store, SCOPE, idem)
            if earlier is not None:
                messages.info(request, "Invoice ini sudah dibuat.")
                return redirect("merchant-sales-invoices")
            override = post.get("credit_override") == "1" and may_override
            try:
                created = batch.create_all(store, plans, actor=request.user, warehouse=warehouse,
                    credit_check=not override, cost_account=_cost_account(store, post.get("cost_account")))
            except InvoiceError as exc:
                idempotency.release(store, SCOPE, idem)
                error = str(exc)
            else:
                idempotency.finish(store, SCOPE, idem, created[-1].pk)
                AuditLog.objects.bulk_create(AuditLog(store=store, actor=request.user, action="invoice.created",
                    object_type="Invoice", object_id=str(invoice.pk), details={"number": invoice.number,
                        "total": str(invoice.total), "mode": "batch"}) for invoice in created)
                first, last = created[0].number, created[-1].number
                messages.success(request, f"{len(created)} invoice dibuat: {first}" + (f" sampai {last}."
                    if len(created) > 1 else "."))
                return redirect("merchant-sales-invoices")
        if error:
            messages.error(request, error)
    customers = list(Customer.objects.filter(store=store).exclude(status="blocked").order_by("name")
        .values("id", "name", "company_name", "phone", "payment_terms_days")[:3000])
    from apps.catalog.models import Product
    products = Product.objects.filter(store=store, is_active=True).exclude(sku="").order_by("name").values_list(
        "sku", "name", "price")[:3000]
    chosen = set(post.getlist("customer[]")) if post is not None else set()
    lines = [{"item": i, "qty": q, "price": p, "discount": d} for i, q, p, d in zip(
        post.getlist("item[]"), post.getlist("qty[]"), post.getlist("price[]"), post.getlist("line_discount[]"),
        strict=False)] if post is not None and mode == "pelanggan" else []
    return render(request, "merchant/invoice_batch.html", _merchant_context(request, store,
        modes=MODES, mode=mode, plans=plans, summary=batch.summary(plans) if plans else None,
        columns=batch.COLUMNS, customers=customers, chosen=chosen, products=products,
        lines=lines or [{"item": "", "qty": "1", "price": "", "discount": ""}] * 2,
        post=post or {}, idem_key=idempotency.new_key(), warehouses=_warehouses(store),
        is_pkp=bool(vat_for(store, Decimal(100))[1]), may_override_credit=may_override,
        today=timezone.localdate().isoformat(), max_invoices=batch.MAX_INVOICES,
        cost_accounts=BankAccount.objects.filter(store=store, is_active=True).order_by("account__code")))


def _cost_account(store, value):
    """The money account the courier was paid from; None (the main cash account) when not chosen."""
    if not (value or "").isdigit():
        return None
    return BankAccount.objects.filter(store=store, is_active=True, pk=value).first()
