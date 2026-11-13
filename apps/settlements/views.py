from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.catalog.views import paginate
from apps.orders.models import Order
from apps.orders.views import _merchant_context, _require_roles, _staff_store

from .models import ConsignmentPartner, PartnerProductPrice, Settlement, SettlementPayment


def bank_accounts_for(store):
    from apps.accounting.bank import bank_accounts
    return bank_accounts(store)

from .services import (
    SettlementError,
    add_sell_through_orders,
    create_settlement,
    record_payment,
    transition,
)

SETTLEMENT_ROLES = {"admin", "manager", "finance"}
VIEW_ROLES = SETTLEMENT_ROLES | {"viewer"}
PAGE_SIZE = 20


# ── Partner management ────────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def settlement_partners(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)

    if request.method == "POST" and request.POST.get("action") == "add_partner":
        _require_roles(request, store, SETTLEMENT_ROLES)
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Nama mitra wajib diisi.")
        else:
            ConsignmentPartner.objects.get_or_create(
                store=store, name=name,
                defaults={
                    "contact_name": request.POST.get("contact_name", ""),
                    "contact_phone": request.POST.get("contact_phone", ""),
                    "payment_term_days": int(request.POST.get("payment_term_days") or 30),
                },
            )
            messages.success(request, f"Mitra '{name}' ditambahkan.")
            return redirect("settlement-partners")

    partners = ConsignmentPartner.objects.filter(store=store).annotate(
        total_settlements=models.Count("settlements"),
        open_total_due=models.Sum(
            "settlements__total_due",
            filter=models.Q(settlements__status__in=[
                Settlement.STATUS_INVOICED, Settlement.STATUS_PARTIAL,
            ]),
        ),
        open_amount_paid=models.Sum(
            "settlements__amount_paid",
            filter=models.Q(settlements__status__in=[
                Settlement.STATUS_INVOICED, Settlement.STATUS_PARTIAL,
            ]),
        ),
    )
    return render(request, "merchant/settlement_partners.html", _merchant_context(
        request, store, partners=partners,
    ))


# ── Settlement list ───────────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def settlement_list(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)

    qs = Settlement.objects.filter(store=store).select_related("partner")

    partner_id = request.GET.get("partner", "")
    if partner_id.isdigit():
        qs = qs.filter(partner_id=int(partner_id))

    status_filter = request.GET.get("status", "")
    if status_filter:
        qs = qs.filter(status=status_filter)

    page, page_range, page_query = paginate(request, qs, PAGE_SIZE)

    # Summary for open settlements
    open_qs = Settlement.objects.filter(
        store=store,
        status__in=[Settlement.STATUS_INVOICED, Settlement.STATUS_PARTIAL],
    )
    open_total = open_qs.aggregate(s=models.Sum("total_due"))["s"] or Decimal(0)
    open_paid = open_qs.aggregate(s=models.Sum("amount_paid"))["s"] or Decimal(0)

    return render(request, "merchant/settlements.html", _merchant_context(
        request, store,
        settlements=page.object_list, page_obj=page, page_range=page_range,
        page_query=page_query,
        partners=ConsignmentPartner.objects.filter(store=store),
        statuses=Settlement.STATUS_CHOICES,
        partner_filter=partner_id, status_filter=status_filter,
        open_receivable=open_total - open_paid,
        open_total=open_total,
    ))


# ── New settlement ─────────────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def settlement_new(request):
    store = _staff_store(request)
    _require_roles(request, store, SETTLEMENT_ROLES)

    partners = ConsignmentPartner.objects.filter(store=store, is_active=True)

    if request.method == "POST":
        try:
            partner_id = int(request.POST.get("partner_id") or 0)
            partner = get_object_or_404(ConsignmentPartner, pk=partner_id, store=store)
            period_start = parse_date(request.POST.get("period_start", ""))
            period_end = parse_date(request.POST.get("period_end", ""))
            if not period_start or not period_end:
                raise SettlementError("Tanggal periode wajib diisi.")
            notes = request.POST.get("notes", "").strip()
            settlement = create_settlement(store, partner, period_start, period_end,
                                           created_by=request.user, notes=notes)

            # Auto-add sell-through orders in that period
            # Only this mitra's orders (or ones with no mitra recorded) that were not billed yet.
            order_ids = list(Order.objects.filter(
                store=store,
                order_type__in=["reseller_sell_in", "reseller_sell_through"],
                created_at__date__gte=period_start,
                created_at__date__lte=period_end,
            ).filter(models.Q(consignment_partner=partner) | models.Q(consignment_partner__isnull=True),
                abshop_billing_status="unbilled").values_list("id", flat=True))
            added = add_sell_through_orders(settlement, order_ids)
            messages.success(request, f"Settlement dibuat dengan {added} pesanan.")
            return redirect("settlement-detail", pk=settlement.pk)
        except (SettlementError, ValueError) as exc:
            messages.error(request, str(exc))

    # Pre-fill unbilled AB Shop orders for context
    unbilled_orders = Order.objects.filter(
        store=store,
        order_type__in=["reseller_sell_in", "reseller_sell_through"],
        abshop_billing_status="unbilled",
    ).order_by("created_at")

    return render(request, "merchant/settlement_new.html", _merchant_context(
        request, store, partners=partners, unbilled_orders=unbilled_orders,
        today=date.today(),
    ))


# ── Settlement detail ─────────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def settlement_detail(request, pk):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    settlement = get_object_or_404(Settlement, pk=pk, store=store)

    if request.method == "POST":
        _require_roles(request, store, SETTLEMENT_ROLES)
        action = request.POST.get("action", "")

        if action == "transition":
            new_status = request.POST.get("new_status", "")
            notes = request.POST.get("notes", "").strip()
            dispute_reason = request.POST.get("dispute_reason", "").strip()
            try:
                transition(settlement, new_status, actor=request.user,
                           notes=notes, dispute_reason=dispute_reason)
                # Mark included sell-in orders as billed when invoiced
                if new_status == Settlement.STATUS_INVOICED:
                    _mark_orders_billed(settlement)
                messages.success(request,
                    f"Status diubah ke '{settlement.get_status_display()}'.")
            except SettlementError as exc:
                messages.error(request, str(exc))

        elif action == "record_payment":
            try:
                amount = Decimal(request.POST.get("amount", "0").replace(",", "").replace(".", ""))
                # Handle Indonesian formatting (1.000.000 or 1000000)
                raw = request.POST.get("amount", "").strip()
                # Strip Rp and spaces
                raw = raw.replace("Rp", "").replace(" ", "").replace(".", "").replace(",", ".")
                amount = Decimal(raw)
                payment_date = parse_date(request.POST.get("payment_date", "")) or date.today()
                method = request.POST.get("method", "bank_transfer")
                reference = request.POST.get("reference", "").strip()
                pay_notes = request.POST.get("pay_notes", "").strip()
                from apps.accounting.bank import bank_accounts
                bank = next((b for b in bank_accounts(store)
                    if str(b.pk) == request.POST.get("bank_account")), None)
                record_payment(settlement, amount, payment_date, method=method,
                               reference=reference, notes=pay_notes,
                               recorded_by=request.user, bank_account=bank)
                messages.success(request, f"Pembayaran Rp{amount:,.0f} dicatat.")
            except (SettlementError, InvalidOperation) as exc:
                messages.error(request, f"Gagal mencatat pembayaran: {exc}")

        elif action == "add_orders":
            if not settlement.is_locked:
                raw_ids = request.POST.getlist("order_ids")
                try:
                    added = add_sell_through_orders(settlement, raw_ids)
                    messages.success(request, f"{added} pesanan ditambahkan.")
                except SettlementError as exc:
                    messages.error(request, str(exc))

        return redirect("settlement-detail", pk=pk)

    lines = settlement.lines.select_related("order").order_by("sale_date")
    payments = settlement.payments.order_by("payment_date")

    # Candidate orders to add (unlocked settlements only)
    candidate_orders = []
    if not settlement.is_locked:
        candidate_orders = Order.objects.filter(
            store=store,
            order_type__in=["reseller_sell_in", "reseller_sell_through"],
            created_at__date__gte=settlement.period_start,
            created_at__date__lte=settlement.period_end,
        ).exclude(settlement_line__settlement=settlement).prefetch_related("items")

    return render(request, "merchant/settlement_detail.html", _merchant_context(
        request, store,
        settlement=settlement,
        lines=lines,
        payments=payments,
        candidate_orders=candidate_orders,
        allowed_transitions=[
            (s, label) for s, label in Settlement.STATUS_CHOICES
            if settlement.can_transition_to(s)
        ],
        payment_methods=SettlementPayment.METHODS, banks=bank_accounts_for(store),
        today=date.today(),
    ))


# ── Partner detail ───────────────────────────────────────────────────────────

@login_required(login_url="/merchant/login/")
def settlement_partner_detail(request, pk):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    partner = get_object_or_404(ConsignmentPartner, pk=pk, store=store)

    if request.method == "POST" and merchant_role_from_request(request, store) in SETTLEMENT_ROLES:
        action = request.POST.get("action", "")
        if action == "edit_partner":
            partner.contact_name = request.POST.get("contact_name", partner.contact_name).strip()
            partner.contact_phone = request.POST.get("contact_phone", partner.contact_phone).strip()
            partner.contact_email = request.POST.get("contact_email", partner.contact_email).strip()
            partner.payment_term_days = int(request.POST.get("payment_term_days") or partner.payment_term_days)
            partner.notes = request.POST.get("notes", partner.notes).strip()
            partner.is_active = request.POST.get("is_active") == "1"
            partner.save()
            messages.success(request, "Data mitra diperbarui.")
            return redirect("settlement-partner-detail", pk=pk)

    settlements = Settlement.objects.filter(store=store, partner=partner).order_by("-period_end")
    orders = Order.objects.filter(
        store=store,
        order_type__in=["reseller_sell_in", "reseller_sell_through"],
    ).order_by("-created_at")

    # Summary
    total_invoiced = settlements.filter(status__in=[
        Settlement.STATUS_INVOICED, Settlement.STATUS_PARTIAL,
        Settlement.STATUS_PAID, Settlement.STATUS_CLOSED,
    ]).aggregate(s=models.Sum("total_due"))["s"] or Decimal(0)
    total_paid = settlements.aggregate(s=models.Sum("amount_paid"))["s"] or Decimal(0)

    page, page_range, page_query = paginate(request, settlements, PAGE_SIZE)

    return render(request, "merchant/settlement_partner_detail.html", _merchant_context(
        request, store,
        partner=partner,
        settlements=page.object_list,
        page_obj=page, page_range=page_range, page_query=page_query,
        recent_orders=orders[:20],
        total_invoiced=total_invoiced,
        total_paid=total_paid,
        balance_due=total_invoiced - total_paid,
        status_choices=Settlement.STATUS_CHOICES,
    ))


def merchant_role_from_request(request, store):
    from apps.shops.models import StaffMembership
    try:
        return StaffMembership.objects.get(store=store, user=request.user).role
    except Exception:
        return "admin"


@login_required(login_url="/merchant/login/")
def product_partner_prices(request, product_id):
    """htmx endpoint — save per-partner sell-in prices for a product."""
    from apps.catalog.models import Product
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager"})
    product = get_object_or_404(Product, pk=product_id, store=store)
    partners = ConsignmentPartner.objects.filter(store=store, is_active=True)

    if request.method == "POST":
        saved = 0
        for partner in partners:
            raw = request.POST.get(f"price_{partner.pk}", "").strip()
            if raw:
                try:
                    price = Decimal(raw.replace(",", ".").replace(".", "", raw.count(".") - 1)
                                    if raw.count(".") > 1 else raw.replace(",", "."))
                    PartnerProductPrice.objects.update_or_create(
                        partner=partner, product=product,
                        defaults={"price": price,
                                  "notes": request.POST.get(f"notes_{partner.pk}", "")[:200]})
                    saved += 1
                except (InvalidOperation, Exception):
                    pass
            else:
                # Empty = remove custom price (fall back to product default)
                PartnerProductPrice.objects.filter(partner=partner, product=product).delete()

        # For htmx: return the updated table partial
        prices = {pp.partner_id: pp for pp in
                  PartnerProductPrice.objects.filter(product=product, partner__store=store)}
        partner_rows = [(p, prices.get(p.pk)) for p in partners]
        return render(request, "merchant/product_partner_prices_partial.html", {
            "product": product, "partner_rows": partner_rows, "saved": saved,
        })

    prices = {pp.partner_id: pp for pp in
              PartnerProductPrice.objects.filter(product=product, partner__store=store)}
    partner_rows = [(p, prices.get(p.pk)) for p in partners]
    return render(request, "merchant/product_partner_prices_partial.html", {
        "product": product, "partner_rows": partner_rows, "saved": None,
    })


def _mark_orders_billed(settlement: Settlement):
    order_ids = settlement.lines.exclude(order=None).values_list("order_id", flat=True)
    Order.objects.filter(pk__in=order_ids, store=settlement.store).update(
        abshop_billing_status="billed"
    )


# ── Mitra Invoice (manual sell-through entry) ─────────────────────────────────

@login_required(login_url="/merchant/login/")
def mitra_invoice_list(request, partner_id):
    """All sell-through invoices for a specific Mitra partner."""
    from django.db.models import Count, Sum
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    partner = get_object_or_404(ConsignmentPartner, id=partner_id, store=store)

    orders = (Order.objects
              .filter(store=store, consignment_partner=partner, channel="abshop")
              .order_by("-created_at"))

    totals = orders.aggregate(revenue=Sum("total"), count=Count("id"))

    page, page_range, page_query = paginate(request, orders, 25)
    return render(request, "merchant/mitra_invoice_list.html",
        _merchant_context(request, store,
            partner=partner,
            orders=page.object_list, page_obj=page, page_range=page_range,
            page_query=page_query,
            revenue=totals["revenue"] or 0,
            order_count=totals["count"] or 0,
        ))


@login_required(login_url="/merchant/login/")
def mitra_invoice_new(request, partner_id):
    """Create a sell-through invoice for a Mitra partner."""
    from decimal import Decimal, InvalidOperation

    from apps.catalog.models import Product
    from apps.orders.models import OrderItem

    store = _staff_store(request)
    _require_roles(request, store, SETTLEMENT_ROLES)
    partner = get_object_or_404(ConsignmentPartner, id=partner_id, store=store)

    products = Product.objects.filter(store=store, is_active=True).order_by("name")

    if request.method == "POST":
        try:
            invoice_date_str = request.POST.get("invoice_date", "")
            invoice_date = parse_date(invoice_date_str) if invoice_date_str else date.today()
            buyer_name = request.POST.get("buyer_name", partner.name)[:120]
            notes = request.POST.get("notes", "")[:300]

            skus = request.POST.getlist("sku[]")
            qtys = request.POST.getlist("qty[]")
            prices = request.POST.getlist("unit_price[]")

            lines = []
            subtotal = Decimal(0)
            for sku, qty_str, price_str in zip(skus, qtys, prices):
                sku = sku.strip()
                if not sku:
                    continue
                try:
                    qty = int(qty_str)
                    unit_price = Decimal(price_str.replace(",", "").replace(".", ""))
                except (ValueError, InvalidOperation):
                    continue
                if qty <= 0 or unit_price < 0:
                    continue
                product = Product.objects.filter(store=store, sku=sku).first()
                if not product:
                    # Try by name
                    product = Product.objects.filter(store=store, name__iexact=sku).first()
                if not product:
                    messages.error(request, f"Produk dengan SKU '{sku}' tidak ditemukan.")
                    return render(request, "merchant/mitra_invoice_new.html",
                        _merchant_context(request, store, partner=partner, products=products))
                line_total = unit_price * qty
                subtotal += line_total
                lines.append({"product": product, "sku": product.sku,
                               "name": product.name, "qty": qty,
                               "unit_price": unit_price, "line_total": line_total})

            if not lines:
                messages.error(request, "Tambahkan minimal satu produk.")
                return render(request, "merchant/mitra_invoice_new.html",
                    _merchant_context(request, store, partner=partner, products=products))

            discount = Decimal(0)
            try:
                discount = Decimal(request.POST.get("discount", "0").replace(",", ""))
            except InvalidOperation:
                pass
            subtotal_net = max(subtotal - discount, Decimal(0))

            from django.db import transaction as db_transaction
            with db_transaction.atomic():
                from apps.orders.models import OrderStatusEvent
                order = Order.objects.create(
                    store=store,
                    channel="abshop",
                    order_type="reseller_sell_through",
                    consignment_partner=partner,
                    customer_name=buyer_name,
                    phone=partner.contact_phone or "-",
                    shipping_address="-",
                    subtotal=subtotal,
                    discount=discount,
                    shipping_cost=Decimal(0),
                    total=subtotal_net,
                    payment_method="mitra",
                    delivery_notes=notes,
                    paid_to_store_account=False,
                )
                # Backdate the order to the invoice date
                Order.objects.filter(pk=order.pk).update(created_at=invoice_date)
                order.refresh_from_db()
                order.save()  # triggers auto invoice_number generation

                for line in lines:
                    OrderItem.objects.create(
                        order=order,
                        product=line["product"],
                        name=line["name"],
                        sku=line["sku"],
                        unit_price=line["unit_price"],
                        quantity=line["qty"],
                        line_total=line["line_total"],
                    )
                OrderStatusEvent.objects.create(
                    order=order, status="paid",
                    note=f"Invoice Mitra dibuat untuk {partner.name}")
                order.status = "paid"
                order.save(update_fields=["status"])

            messages.success(request, f"Invoice {order.invoice_number} berhasil dibuat.")
            return redirect("mitra-invoice-list", partner_id=partner.id)

        except Exception as exc:
            messages.error(request, f"Gagal membuat invoice: {exc}")

    return render(request, "merchant/mitra_invoice_new.html",
        _merchant_context(request, store, partner=partner, products=products,
                          today=date.today().isoformat()))


# ── Stok di mitra (konsinyasi) ───────────────────────────────────────────────

def _find_item(store, sku):
    """The product, and variant if the SKU is a variant's, for a SKU typed by the user."""
    from apps.catalog.models import Product, ProductVariant
    sku = (sku or "").strip()
    variant = ProductVariant.objects.select_related("product").filter(product__store=store,
        sku=sku).first() if sku else None
    if variant:
        return variant.product, variant
    return Product.objects.filter(store=store, sku=sku).first(), None


@login_required(login_url="/merchant/login/")
def partner_stock(request):
    """Barang konsinyasi yang sedang dipegang mitra, plus kirim dan retur."""
    from .consignment import (
        ConsignmentError,
        return_from_partner,
        send_to_partner,
        stock_at_partners,
    )
    from .models import ConsignmentMovement

    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    partners = ConsignmentPartner.objects.filter(store=store, is_active=True)
    if request.method == "POST":
        _require_roles(request, store, SETTLEMENT_ROLES)
        partner = get_object_or_404(partners, pk=request.POST.get("partner") or 0)
        product, variant = _find_item(store, request.POST.get("sku"))
        action = request.POST.get("action")
        try:
            quantity = int(request.POST.get("quantity") or 0)
            if not product:
                raise ConsignmentError("SKU tidak ditemukan di katalog.")
            move = send_to_partner if action == "send" else return_from_partner
            move(partner, product=product, variant=variant, quantity=quantity,
                date=parse_date(request.POST.get("date", "")),
                note=request.POST.get("note", ""), actor=request.user)
            messages.success(request, f"{quantity} × {product.name} "
                + ("dikirim ke " if action == "send" else "diretur dari ") + partner.name + ".")
        except (ConsignmentError, ValueError) as error:
            messages.error(request, f"Gagal: {error}")
        return redirect("partner-stock")

    rows = stock_at_partners(store)
    recent = ConsignmentMovement.objects.filter(store=store).select_related(
        "stock__partner", "stock__product", "stock__variant")[:30]
    return render(request, "merchant/partner_stock.html", _merchant_context(request, store,
        rows=rows, recent=recent, partners=partners, today=date.today().isoformat(),
        total_value=sum((row["value"] for row in rows), Decimal(0)),
        can_edit=_can_edit(request, store)))


def _can_edit(request, store):
    return store.owner_id == request.user.id or store.staff_members.filter(
        user=request.user, role__in=SETTLEMENT_ROLES).exists()
