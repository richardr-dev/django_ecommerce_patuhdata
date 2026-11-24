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
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog, Customer
from apps.shops.permissions import can, roles

from . import invoice_batch as batch
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


@login_required(login_url="/login/")
def merchant_invoice_batch(request):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    post = request.POST if request.method == "POST" else None
    mode = (post or request.GET).get("cara", "tempel")
    mode = mode if mode in dict(MODES) else "tempel"
    plans, error = None, ""
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
                    credit_check=not override)
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
        today=timezone.localdate().isoformat(), max_invoices=batch.MAX_INVOICES))
