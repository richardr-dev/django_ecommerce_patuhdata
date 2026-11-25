"""Impor penerimaan (``tax/receipt_import.py``): payments from an Excel/CSV file, checked first, then saved all
together. The page works on the rows as text: an uploaded file fills the box, "Periksa" shows what will be made,
"Impor" plans again on the server and makes every receipt or none."""
import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.accounting import bank as bank_service
from apps.accounting import idempotency
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog
from apps.shops.permissions import roles

from . import receipt_import, receipts, sheet_import

SCOPE = "receipt_import"


@login_required(login_url="/login/")
def merchant_receipt_import_template(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="template-penerimaan.csv"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(sheet_import.RECEIPT_COLUMNS)
    writer.writerow(["01/06/2026", "C-0001", "", "INV/2026/0001", 5000000, "Transfer Bank", "", "Pelunasan Mei"])
    writer.writerow(["02/06/2026", "C-0002", "RC-77", "INV/2026/0002", 18500000, "Tunai", "", ""])
    writer.writerow(["", "", "", "INV/2026/0003", 300000, "", "", ""])
    return response


@login_required(login_url="/login/")
def merchant_receipt_import(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    banks = list(bank_service.bank_accounts(store))
    post = request.POST.copy() if request.method == "POST" else None
    plans, error = None, ""
    if post is not None and post.get("action") == "upload":
        try:
            upload = request.FILES.get("file")
            if not upload:
                raise ValueError("Pilih file Excel atau CSV terlebih dahulu.")
            post["rows"], notes = sheet_import.receipt_rows(upload)
            post["action"] = "preview"
            for note in notes:
                messages.info(request, note)
        except ValueError as exc:
            messages.error(request, str(exc))
            post = None
    if post is not None:
        default_bank = next((b for b in banks if str(b.pk) == post.get("bank")), None)
        plans = receipt_import.plan(store, post.get("rows", ""), default_bank=default_bank)
        if not plans:
            error = "Tidak ada baris untuk diperiksa."
        elif post.get("action") == "create":
            idem = post.get("idem", "")
            if idempotency.claim(store, SCOPE, idem) is not None:
                messages.info(request, "Penerimaan ini sudah dibuat.")
                return redirect("merchant-receipts")
            try:
                made = receipt_import.create_all(store, plans, actor=request.user)
            except receipts.ReceiptError as exc:
                idempotency.release(store, SCOPE, idem)
                error = str(exc)
            else:
                idempotency.finish(store, SCOPE, idem, made[-1].pk)
                AuditLog.objects.create(store=store, actor=request.user, action="receipts.imported",
                    object_type="CustomerReceipt", object_id="bulk", details={"count": len(made)})
                messages.success(request, f"{len(made)} penerimaan pembayaran dibuat.")
                return redirect("merchant-receipts")
        if error:
            messages.error(request, error)
    return render(request, "merchant/receipt_import.html", _merchant_context(request, store,
        plans=plans, summary=receipt_import.summary(plans) if plans else None, banks=banks,
        post=post or {}, idem_key=idempotency.new_key(), columns=sheet_import.RECEIPT_COLUMNS))
