"""The one invoice form: new, edit and duplicate are the same page, and a customer can be added
to the CRM from inside it."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from apps.accounting.bank import parse_amount
from apps.accounting.ledger import LedgerError
from apps.catalog.models import Product, annotate_available_stock
from apps.orders.models import Order
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog, Customer

from .models import Invoice
from .sales import (
    clean_lines,
    create_sales_invoice,
    invoice_lines,
    invoice_locks,
    line_net,
    update_sales_invoice,
)
from .services import InvoiceError, normalize_npwp, vat_for

EDIT_ROLES = {"admin", "manager", "ops", "finance"}
LOGIN = "/merchant/login/"
TERMS = [("0", "Lunas saat diterima"), ("7", "7 hari"), ("14", "14 hari"), ("30", "30 hari"),
    ("45", "45 hari"), ("60", "60 hari")]


def _number(text, default=Decimal(0)):
    text = str(text or "").strip()
    if not text:
        return default
    parsed = parse_amount(text)
    if parsed is None:
        raise InvalidOperation(text)
    return parsed


def _customer(store, value):
    if not value:
        return None
    try:
        return Customer.objects.filter(store=store, pk=value).first()
    except (ValueError, ValidationError):
        return None


def _post_lines(request, store):
    """The submitted rows. A row with a product id is stock; one without is a service or fee."""
    post = request.POST
    count = len(post.getlist("product[]"))

    def column(name):
        values = post.getlist(name)
        return values + [""] * (count - len(values))
    lines = []
    for product_id, text, qty, price, discount, unit, note, code in zip(
            column("product[]"), column("description[]"), column("qty[]"), column("unit_price[]"),
            column("line_discount[]"), column("unit[]"), column("note[]"), column("code[]"),
            strict=False):
        product = Product.objects.filter(store=store, pk=product_id).first() if product_id else None
        if not product and not text.strip():
            continue
        lines.append({"product": product, "description": text.strip(),
            "qty": _number(qty, Decimal(1)), "unit_price": _number(price),
            "discount": _number(discount), "unit": unit, "note": note, "code": code})
    return lines


def _rows(lines):
    """Lines as the template needs them to draw (and redraw after an error)."""
    return [{"product_id": line["product"].pk if line.get("product") else "",
        "text": line["description"] if not line.get("product") else (
            f"{line['product'].sku} — {line['product'].name}"),
        "qty": f"{Decimal(line['qty']).normalize():f}", "price": f"{Decimal(line['unit_price']):.0f}",
        "discount": f"{Decimal(line.get('discount') or 0):.0f}" if line.get("discount") else "",
        "net": f"{line_net(line):.0f}", "unit": line.get("unit", ""), "note": line.get("note", ""),
        "code": line.get("code", "")} for line in lines]


def _catalog(store):
    products = annotate_available_stock(Product.objects.filter(store=store, is_active=True)
        ).order_by("name")[:3000]
    return [{"id": str(p.pk), "sku": p.sku, "name": p.name, "price": f"{p.price:.0f}",
        "stock": getattr(p, "stock_available", None)} for p in products]


def _initial(store, source, mode, prefill):
    """What the form starts with: the invoice being edited or copied, a customer, or nothing."""
    if source is not None:
        order = source.order
        buyer = source.buyer_name
        company = ""
        if " (" in buyer and buyer.endswith(")") and order.customer_id and (
                order.customer.company_name and buyer.startswith(order.customer.company_name)):
            company = order.customer.company_name
            buyer = order.customer_name
        return {"customer": order.customer, "buyer_name": buyer if mode == "edit" else
            (order.customer.name if order.customer else buyer), "company_name": company or
            (order.customer.company_name if order.customer else ""), "npwp": source.buyer_npwp,
            "phone": order.phone if order.phone != "-" else "", "address": source.buyer_address
            if source.buyer_address != "-" else "", "channel": order.channel,
            "reference": order.external_order_id, "notes": source.notes,
            "issued_on": source.issued_on if mode == "edit" else timezone.localdate(),
            "due_date": source.due_date if mode == "edit" else None,
            "discount": f"{source.discount:.0f}" if source.discount else "",
            "shipping": f"{source.shipping:.0f}" if source.shipping else "",
            "on_credit": source.status != Invoice.STATUS_PAID if mode == "edit" else False,
            "lines": _rows(invoice_lines(source))}
    return {"customer": prefill, "buyer_name": prefill.name if prefill else "",
        "company_name": prefill.company_name if prefill else "", "npwp": prefill.npwp if prefill else "",
        "phone": prefill.phone if prefill else "", "address": prefill.address if prefill else "",
        "channel": "manual", "reference": "", "notes": "", "issued_on": timezone.localdate(),
        "due_date": None, "discount": "", "shipping": "", "on_credit": False, "lines": []}


@login_required(login_url=LOGIN)
def merchant_invoice_form(request, invoice_id=None, mode="new"):
    """New invoice, edit one, or duplicate one: the same page."""
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    source = get_object_or_404(Invoice.objects.select_related("order__customer"), pk=invoice_id,
        store=store) if invoice_id else None
    editing = mode == "edit"
    locks = invoice_locks(source) if editing else []
    if editing and source.status == Invoice.STATUS_VOID:
        messages.error(request, "Invoice yang dibatalkan tidak bisa diubah.")
        return redirect("merchant-invoice-detail", invoice_id=source.pk)
    initial = _initial(store, source, mode, _customer(store, request.GET.get("customer")))
    if request.method == "POST":
        post = request.POST
        customer = _customer(store, post.get("customer_id"))
        try:
            lines = _post_lines(request, store) if not locks else None
            if lines is not None:
                clean_lines(lines)
            common = {"buyer_name": post.get("buyer_name", "").strip() or (
                customer.name if customer else "Pelanggan"), "company_name": post.get("company_name", ""),
                "buyer_npwp": normalize_npwp(post.get("buyer_npwp", "")), "phone": post.get("phone", "-"),
                "customer": customer, "notes": post.get("notes", ""),
                "issued_on": parse_date(post.get("issued_on", "")) or timezone.localdate()}
            due = parse_date(post.get("due_date", ""))
            reference = post.get("reference", "")
            if editing:
                invoice = update_sales_invoice(source, **common, buyer_address=post.get("address", ""),
                    due_date=due, external_order_id=reference, lines=lines,
                    discount=_number(post.get("discount")) if lines is not None else None,
                    shipping=_number(post.get("shipping")) if lines is not None else None,
                    actor=request.user)
            else:
                invoice = create_sales_invoice(store, lines=lines, address=post.get("address", "-"),
                    channel=post.get("channel", "manual"), external_order_id=reference,
                    discount=_number(post.get("discount")), shipping=_number(post.get("shipping")),
                    on_credit=post.get("payment_status") == "credit", due_date=due,
                    actor=request.user, **{k: v for k, v in common.items() if k != "notes"},
                    notes=common["notes"])
        except (InvoiceError, LedgerError, InvalidOperation, ValueError) as error:
            messages.error(request, f"Gagal: {error}" if str(error) else "Isian tidak valid.")
            initial.update({"customer": customer, "buyer_name": post.get("buyer_name", ""),
                "company_name": post.get("company_name", ""), "npwp": post.get("buyer_npwp", ""),
                "phone": post.get("phone", ""), "address": post.get("address", ""),
                "channel": post.get("channel", "manual"), "reference": post.get("reference", ""),
                "notes": post.get("notes", ""), "discount": post.get("discount", ""),
                "shipping": post.get("shipping", ""), "on_credit": post.get("payment_status") == "credit",
                "issued_on": parse_date(post.get("issued_on", "")) or timezone.localdate(),
                "due_date": parse_date(post.get("due_date", ""))})
            try:
                initial["lines"] = _rows(_post_lines(request, store))
            except InvalidOperation:
                pass
        else:
            AuditLog.objects.create(store=store, actor=request.user,
                action="invoice.updated" if editing else "invoice.created", object_type="Invoice",
                object_id=str(invoice.pk), details={"number": invoice.number,
                    "total": str(invoice.total), "mode": mode})
            messages.success(request, f"Invoice {invoice.number} {'diperbarui' if editing else 'dibuat'}.")
            if post.get("next") == "new" and not editing:
                return redirect("merchant-invoice-new")
            return redirect("merchant-invoice-detail", invoice_id=invoice.pk)
    vat_rate, _ = vat_for(store, Decimal(100), is_pkp=source.seller_is_pkp if editing else None)
    return render(request, "merchant/invoice_form.html", _merchant_context(request, store,
        mode=mode, source=source, locks=locks, initial=initial, terms=TERMS, vat_rate=vat_rate,
        channels=[c for c in Order.CHANNELS if c[0] != "pos"],
        catalog=_catalog(store),
        today=timezone.localdate().isoformat(), post=request.POST))


@login_required(login_url=LOGIN)
@require_POST
def merchant_customer_quick_add(request):
    """Add a customer to the CRM from inside the invoice form. Returns the customer as JSON; a
    phone number already on file returns that customer instead of a duplicate."""
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    name = request.POST.get("name", "").strip()[:120]
    if not name:
        return JsonResponse({"error": "Nama pelanggan wajib diisi."}, status=400)
    try:
        npwp = normalize_npwp(request.POST.get("npwp", ""))
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    phone = request.POST.get("phone", "").strip()[:30]
    existing = Customer.objects.filter(store=store, phone=phone).first() if phone else None
    customer, created = (existing, False) if existing else (Customer.objects.create(store=store,
        name=name, phone=phone, email=request.POST.get("email", "").strip()[:254],
        company_name=request.POST.get("company_name", "").strip()[:160], npwp=npwp,
        address=request.POST.get("address", "").strip(), status=Customer.STATUS_ACTIVE), True)
    if created:
        AuditLog.objects.create(store=store, actor=request.user, action="customer.created",
            object_type="Customer", object_id=str(customer.pk),
            details={"name": name, "from": "invoice form"})
    return JsonResponse({"created": created, "customer": {"id": str(customer.pk),
        "name": customer.name, "company_name": customer.company_name, "phone": customer.phone,
        "email": customer.email, "npwp": customer.npwp, "address": customer.address}})
