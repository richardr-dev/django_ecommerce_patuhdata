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

from apps.accounting import idempotency
from apps.accounting.bank import parse_amount
from apps.accounting.ledger import LedgerError
from apps.catalog.models import (
    PriceListItem,
    Product,
    ProductUnit,
    ProductVariant,
    annotate_available_stock,
)
from apps.orders.models import Order
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog, Customer
from apps.shops.permissions import can, roles

from . import credit_limit
from .models import Invoice
from .sales import (
    clean_lines,
    create_sales_invoice,
    from_inclusive,
    invoice_lines,
    invoice_locks,
    line_net,
    update_sales_invoice,
)
from .services import InvoiceError, exclusive_price, normalize_npwp, vat_for

EDIT_ROLES = roles("sales.edit")
LOGIN = "/login/"
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


def _partners(store):
    from apps.settlements.models import ConsignmentPartner
    return list(ConsignmentPartner.objects.filter(store=store, is_active=True).order_by("name"))


def _partner(store, value):
    from apps.settlements.models import ConsignmentPartner
    try:
        return ConsignmentPartner.objects.filter(store=store, pk=value, is_active=True).first() if value else None
    except (ValueError, ValidationError):
        return None


def _post_lines(request, store):
    """The submitted rows. A row with a product id is stock; one without is a service or fee."""
    from apps.settlements.pricing import partner_price
    post = request.POST
    partner = _partner(store, post.get("partner_id"))
    count = len(post.getlist("product[]"))

    def column(name):
        values = post.getlist(name)
        return values + [""] * (count - len(values))
    lines = []
    for product_id, text, qty, price, discount, unit, note, code in zip(
            column("product[]"), column("description[]"), column("qty[]"), column("unit_price[]"),
            column("line_discount[]"), column("unit[]"), column("note[]"), column("code[]"),
            strict=False):
        product, variant = _item(store, product_id)
        if not product and not text.strip():
            continue
        unit_price = _number(price)
        if not unit_price and product and partner is not None:
            unit_price = partner_price(partner, product, variant) or unit_price  # the mitra's own price
        lines.append({"product": product, "variant": variant, "description": text.strip(),
            "qty": _number(qty, Decimal(1)), "unit_price": unit_price,
            "discount": _number(discount), "unit": unit, "note": note, "code": code})
    return lines


def _item(store, value):
    """``(product, variant)`` of a posted ``product[]``: a product id, or ``v<id>`` for a size or colour."""
    value = (value or "").strip()
    if not value:
        return None, None
    if value.startswith("v"):
        variant = ProductVariant.objects.select_related("product").filter(product__store=store,
            pk=value[1:] if value[1:].isdigit() else None).first()
        if variant is None:
            raise InvoiceError("Varian produk tidak ditemukan.")
        return variant.product, variant
    return Product.objects.filter(store=store, pk=value).first(), None


def _rows(lines):
    """Lines as the template needs them to draw (and redraw after an error)."""
    return [{"product_id": (f"v{line['variant'].pk}" if line.get("variant") else line["product"].pk)
        if line.get("product") else "",
        "text": line["description"] if not line.get("product") else _label(line["product"], line.get("variant")),
        "qty": f"{Decimal(line['qty']).normalize():f}", "price": f"{Decimal(line['unit_price']):.0f}",
        "discount": f"{Decimal(line.get('discount') or 0):.0f}" if line.get("discount") else "",
        "net": f"{line_net(line):.0f}", "unit": line.get("unit", ""), "note": line.get("note", ""),
        "code": line.get("code", "")} for line in lines]


def _with_tax(store, amount):
    """A price before PPN as the seller typed it with PPN in: the whole rupiah that ``exclusive_price``
    brings back to the same figure (else the exact cents)."""
    amount = Decimal(amount or 0)
    if not amount or not store.tax_rate:
        return amount
    gross = amount * (100 + store.tax_rate) / 100
    whole = gross.quantize(Decimal(1))
    return whole if exclusive_price(store, whole) == amount else gross.quantize(Decimal("0.01"))


def _typed_with_tax(store, lines, discount):
    """Lines and invoice discount of an invoice entered with "Harga sudah termasuk PPN", put back the
    way they were typed so the edit form shows the same figures."""
    lines = [{**line, "unit_price": _with_tax(store, line["unit_price"]),
        "discount": _with_tax(store, line.get("discount") or 0)} for line in lines]
    return lines, _with_tax(store, discount)


def _label(product, variant=None):
    """How a product (or one of its sizes) reads in the picker; the form matches typed text against it."""
    if variant is not None:
        return f"{variant.sku} — {product.name} / {variant.name}"
    return f"{product.sku} — {product.name}"


def _catalog(store):
    """Products for the picker. A product with variants is sold through its sizes or colours, each its own
    entry with its own stock; the parent stays in the list (``pick: False``) only so older lines still read."""
    from apps.settlements.models import PartnerProductPrice
    products = annotate_available_stock(Product.objects.filter(store=store, is_active=True)
        ).order_by("name")[:3000]
    units = {}   # larger units (dus = 12 pcs): the form prices a line by the unit chosen
    for product_id, name, factor in ProductUnit.objects.filter(product__store=store, is_active=True).values_list(
            "product_id", "name", "factor"):
        units.setdefault(product_id, []).append({"name": name, "factor": factor})
    variants = {}
    for variant in ProductVariant.objects.filter(product__store=store, product__is_active=True,
            is_active=True).order_by("product_id", "name"):
        variants.setdefault(variant.product_id, []).append(variant)
    variant_lists = {}
    for variant_id, list_id, price in PriceListItem.objects.filter(price_list__store=store,
            price_list__is_active=True, variant__isnull=False).values_list("variant_id", "price_list_id", "price"):
        variant_lists.setdefault(variant_id, {})[str(list_id)] = f"{price:.0f}"
    mitra = {}
    for product_id, partner_id, price in PartnerProductPrice.objects.filter(
            partner__store=store).values_list("product_id", "partner_id", "price"):
        mitra.setdefault(product_id, {})[str(partner_id)] = f"{price:.0f}"
    from .pricing import price_map
    lists = price_map(store)
    found = []
    for p in products:
        sizes = variants.get(p.pk, [])
        found.append({"id": str(p.pk), "label": _label(p), "sku": p.sku, "name": p.name, "price": f"{p.price:.0f}",
            "base": p.base_unit or "pcs", "units": units.get(p.pk, []),
            "ab_price": f"{p.ab_shop_price:.0f}" if p.ab_shop_price is not None else "",
            "mitra": mitra.get(p.pk, {}), "lists": lists.get(p.pk, {}), "pick": not sizes,
            "stock": getattr(p, "stock_available", None)})
        for v in sizes:   # the parent's list and mitra prices apply unless the variant has its own row
            found.append({"id": f"v{v.pk}", "label": _label(p, v), "sku": v.sku, "name": f"{p.name} / {v.name}",
                "price": f"{v.effective_price:.0f}", "ab_price": f"{v.effective_ab_shop_price:.0f}"
                if v.ab_shop_price is not None or p.ab_shop_price is not None else "",
                "mitra": mitra.get(p.pk, {}), "lists": {**lists.get(p.pk, {}), **variant_lists.get(v.pk, {})},
                "pick": True, "stock": v.available_stock})
    return found


def _initial(store, source, mode, prefill):
    """What the form starts with: the invoice being edited or copied, a customer, or nothing."""
    if source is not None:
        order = source.order
        buyer = source.buyer_name
        company = ""
        lines, discount = invoice_lines(source), source.discount
        if source.prices_include_tax and source.seller_is_pkp:
            lines, discount = _typed_with_tax(store, lines, discount)
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
            "courier": order.courier_code if mode == "edit" else "",
            "tracking_number": order.tracking_number if mode == "edit" else "",
            "partner_id": order.consignment_partner_id or "",
            "issued_on": source.issued_on if mode == "edit" else timezone.localdate(),
            "due_date": source.due_date if mode == "edit" else None,
            "discount": f"{discount:.0f}" if discount else "",
            "prices_include_tax": source.prices_include_tax,
            "shipping": f"{source.shipping:.0f}" if source.shipping else "",
            "on_credit": source.status != Invoice.STATUS_PAID if mode == "edit" else False,
            "warehouse": str(source.warehouse_id or ""),   # a copy sells from the same location
            "lines": _rows(lines)}
    return {"customer": prefill, "buyer_name": prefill.name if prefill else "",
        "company_name": prefill.company_name if prefill else "", "npwp": prefill.npwp if prefill else "",
        "phone": prefill.phone if prefill else "", "address": prefill.address if prefill else "",
        "channel": "manual", "reference": "", "notes": "", "issued_on": timezone.localdate(),
        "courier": "", "tracking_number": "", "partner_id": "", "due_date": None, "discount": "", "shipping": "", "on_credit": False, "lines": []}


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
        idem = "" if editing else post.get("idem", "")
        earlier = idempotency.claim(store, "invoice", idem)
        if earlier is not None:  # the same form was already submitted: do not invoice twice
            messages.info(request, "Invoice ini sudah dibuat.")
            return redirect("merchant-invoice-detail", invoice_id=earlier) if earlier else redirect("merchant-sales-invoices")
        customer = _customer(store, post.get("customer_id"))
        # A manager or finance may sell past the customer's credit limit; everyone else is held to it.
        override = post.get("credit_override") == "1" and can(request.user, store, "sales.credit")
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
            if due is None and post.get("payment_status") == "credit" and not editing:
                from .pricing import due_date_for  # the customer's terms fill a due date left empty
                due = due_date_for(customer, common["issued_on"])
            reference = post.get("reference", "")
            inclusive = post.get("prices_include_tax") == "1" and lines is not None
            discount, rounding = _number(post.get("discount")), Decimal(0)
            if inclusive:   # prices typed with PPN in them
                lines, discount, rounding = from_inclusive(store, lines, discount)
            if editing:
                invoice = update_sales_invoice(source, **common, buyer_address=post.get("address", ""),
                    due_date=due, external_order_id=reference, lines=lines,
                    discount=discount if lines is not None else None,
                    shipping=_number(post.get("shipping")) if lines is not None else None,
                    rounding=rounding, prices_include_tax=inclusive if lines is not None else None,
                    actor=request.user, credit_check=not override)
                Order.objects.filter(pk=invoice.order_id).update(
                    courier_code=post.get("courier", "").strip().lower()[:40],
                    tracking_number=post.get("tracking_number", "").strip()[:100])
            else:
                invoice = create_sales_invoice(store, lines=lines, address=post.get("address", "-"),
                    channel=post.get("channel", "manual"), external_order_id=reference,
                    discount=discount, shipping=_number(post.get("shipping")), rounding=rounding,
                    warehouse=_warehouse(store, post.get("warehouse")),
                    on_credit=post.get("payment_status") == "credit", due_date=due,
                    actor=request.user, **{k: v for k, v in common.items() if k != "notes"},
                    notes=common["notes"], courier=post.get("courier", ""),
                    tracking_number=post.get("tracking_number", ""),
                    partner=_partner(store, post.get("partner_id")), credit_check=not override,
                    prices_include_tax=inclusive)
        except (InvoiceError, LedgerError, InvalidOperation, ValueError) as error:
            idempotency.release(store, "invoice", idem)
            messages.error(request, f"Gagal: {error}" if str(error) else "Isian tidak valid.")
            initial.update({"customer": customer, "buyer_name": post.get("buyer_name", ""),
                "company_name": post.get("company_name", ""), "npwp": post.get("buyer_npwp", ""),
                "phone": post.get("phone", ""), "address": post.get("address", ""),
                "channel": post.get("channel", "manual"), "reference": post.get("reference", ""),
                "courier": post.get("courier", ""), "tracking_number": post.get("tracking_number", ""),
                "partner_id": post.get("partner_id", ""),
                "notes": post.get("notes", ""), "discount": post.get("discount", ""),
                "shipping": post.get("shipping", ""), "on_credit": post.get("payment_status") == "credit",
                "issued_on": parse_date(post.get("issued_on", "")) or timezone.localdate(),
                "due_date": parse_date(post.get("due_date", "")),
                "prices_include_tax": post.get("prices_include_tax") == "1", "warehouse": post.get("warehouse", "")})
            try:
                initial["lines"] = _rows(_post_lines(request, store))
            except (InvalidOperation, InvoiceError):
                pass
        else:
            idempotency.finish(store, "invoice", idem, invoice.pk)
            if override and customer is not None:
                credit_limit.log_override(store, request.user, invoice, customer)
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
        price_lists=__import__("apps.tax.pricing", fromlist=["lists_for_form"]).lists_for_form(store),
        channels=[c for c in Order.CHANNELS if c[0] not in {"pos", "abshop"}],
        catalog=_catalog(store), partners=_partners(store), idem_key=idempotency.new_key(),
        may_override_credit=can(request.user, store, "sales.credit"),
        today=timezone.localdate().isoformat(), post=request.POST, warehouses=_warehouses(store)))


def _warehouses(store):
    """Locations to sell from; the choice is offered only when there is more than one."""
    from apps.catalog.models import Warehouse
    found = list(Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id"))
    return found if len(found) > 1 else []


def _warehouse(store, value):
    """The chosen location, or None: the store's locations in priority order."""
    if not value:
        return None
    from apps.catalog.models import Warehouse
    found = Warehouse.objects.filter(store=store, is_active=True, pk=value).first() if str(value).isdigit() else None
    if found is None:
        raise InvoiceError("Gudang tidak ditemukan.")
    return found


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
