"""Kasir (POS) pages. Logic lives in pos/services.py.

Two front doors reach the same registers and shifts. ``/books/pos/`` is the owner's control panel
(inside the back office: create registers, outlet settings, see every shift) and stays restricted to
roles that manage the store. ``/kasir/`` is the outlet's own app: its own sign-in, no back-office
chrome at any step, so a tablet left open at the counter never shows the books. Both post to the same
endpoints (``merchant_pos_open``, ``merchant_pos_sell``, ``merchant_pos_shift``), which is what keeps
them one system rather than two."""
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounting import bank
from apps.catalog.models import Product, Warehouse
from apps.orders.forms import MerchantLoginForm
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops import permissions
from apps.shops.audit import trail as audit_trail
from apps.shops.auth import sign_out
from apps.shops.models import Customer
from apps.shops.permissions import roles
from apps.tax.services import tax_profile

from . import services as pos
from .models import PosRegister, PosSale, PosShift, PosTender

LOGIN = "/login/"
SELL_ROLES = roles("pos.sell")
SHIFT_ROLES = roles("pos.shift")
SETTINGS_ROLES = roles("pos.settings")
APPROVE_ROLES = roles("pos.shift_approve")
VIEW_ROLES = SELL_ROLES | SHIFT_ROLES | SETTINGS_ROLES | APPROVE_ROLES
# Who may open the admin control panel (/books/pos/): the roles that manage registers, not a plain
# cashier or ops hand whose job is only to sell. They reach everything through /kasir/ instead.
ADMIN_ROLES = SETTINGS_ROLES | APPROVE_ROLES


def _register(request, store, register_id):
    """The register, only when this person may serve it (a cashier is pinned to one outlet)."""
    register = pos.registers_for(store, request.user).filter(pk=register_id).first()
    if register is None:
        raise Http404
    return register


def _account(store, value):
    return bank.BankAccount.objects.filter(store=store, pk=value, is_active=True).first() if value else None


@login_required(login_url=LOGIN)
def merchant_pos(request):
    store = _staff_store(request)
    _require_roles(request, store, ADMIN_ROLES)
    registers = list(pos.registers_for(store, request.user))
    open_shifts = {shift.register_id: shift for shift in PosShift.objects.filter(register__in=registers,
        status=PosShift.OPEN).select_related("opened_by")}
    rows = []
    for register in registers:
        shift = open_shifts.get(register.id)
        rows.append({"register": register, "shift": shift, "balance": pos.drawer_balance(register),
            "summary": pos.shift_summary(shift) if shift else None})
    drawers = {register.cash_account_id for register in registers}
    shifts = PosShift.objects.filter(register__in=registers, status=PosShift.CLOSED).select_related(
        "register__outlet", "closed_by")[:30]
    return render(request, "merchant/pos.html", _merchant_context(request, store, rows=rows, shifts=shifts,
        outlets=Warehouse.objects.filter(store=store, is_active=True, is_outlet=True),
        outlet_settings=[pos.settings_for(outlet) for outlet in {row["register"].outlet for row in rows}],
        rounding_choices=pos.PosOutletSettings.ROUNDING,
        accounts=[a for a in bank.bank_accounts(store) if a.id not in drawers],
        can_settings=permissions.can(request.user, store, "pos.settings"),
        can_shift=permissions.can(request.user, store, "pos.shift"),
        can_sell=permissions.can(request.user, store, "pos.sell")))


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_register_new(request):
    store = _staff_store(request)
    _require_roles(request, store, SETTINGS_ROLES)
    outlet = Warehouse.objects.filter(store=store, pk=request.POST.get("outlet") or 0).first()
    try:
        register = pos.create_register(store, outlet=outlet, code=request.POST.get("code"),
            name=request.POST.get("name", ""), actor=request.user)
    except pos.PosError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Register {register.code} dibuat dengan akun kas {register.cash_account.name}.")
    return redirect("merchant-pos")


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_settings(request, outlet_id):
    store = _staff_store(request)
    _require_roles(request, store, SETTINGS_ROLES)
    outlet = get_object_or_404(Warehouse, store=store, pk=outlet_id, is_outlet=True)
    try:
        pos.update_settings(outlet, prices_include_tax=request.POST.get("prices_include_tax") == "1",
            rounding_step=request.POST.get("rounding_step"), max_discount_pct=request.POST.get("max_discount_pct"),
            receipt_header=request.POST.get("receipt_header", ""), receipt_footer=request.POST.get("receipt_footer", ""),
            actor=request.user)
    except pos.PosError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Pengaturan kasir {outlet.name} disimpan.")
    return redirect("merchant-pos")


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_open(request, register_id):
    store = _staff_store(request)
    _require_roles(request, store, SHIFT_ROLES)
    register = _register(request, store, register_id)
    try:
        pos.open_shift(register, opening_float=bank.parse_amount(request.POST.get("opening_float") or "0") or 0,
            actor=request.user, fund_from=_account(store, request.POST.get("fund_from")))
    except pos.PosError as error:
        messages.error(request, str(error))
        return redirect("merchant-pos")
    return redirect("merchant-pos-sell", register.pk)


@login_required(login_url=LOGIN)
def merchant_pos_sell(request, register_id):
    """The register screen (GET) and ringing up one receipt (POST, JSON in and out)."""
    store = _staff_store(request)
    _require_roles(request, store, SELL_ROLES)
    register = _register(request, store, register_id)
    if request.method == "POST":
        return _ring_up(request, store, register)
    shift = pos.open_shift_of(register)
    if shift is None:
        messages.error(request, "Buka shift kasir lebih dulu.")
        return redirect("merchant-pos")
    config, drawers = pos.settings_for(register.outlet), set(PosRegister.objects.filter(store=store).values_list(
        "cash_account_id", flat=True))
    profile = tax_profile(store)
    setup = {"products": pos.catalog(store, register.outlet), "rounding": config.rounding_step,
        "inclusive": bool(config.prices_include_tax), "taxRate": float(store.tax_rate if profile.is_pkp else 0),
        "maxDiscount": float(config.max_discount_pct),
        "override": permissions.can(request.user, store, "pos.discount_override"),
        "accounts": [{"id": a.id, "name": a.name} for a in bank.bank_accounts(store) if a.id not in drawers]}
    return render(request, "merchant/pos_sell.html", {"merchant_store": store, "register": register, "shift": shift,
        "setup": setup, "summary": pos.shift_summary(shift)})


def _ring_up(request, store, register):
    try:
        data = json.loads(request.body or b"{}")
        products = Product.objects.filter(store=store).in_bulk([int(line["id"]) for line in data.get("lines", [])])
        lines = [{"product": products.get(int(line["id"])), "qty": line.get("qty"), "unit_price": line.get("price"),
            "discount": line.get("discount")} for line in data.get("lines", [])]
        tenders = [{"method": t.get("method"), "amount": t.get("amount"), "reference": t.get("reference", ""),
            "bank_account": _account(store, t.get("account"))} for t in data.get("tenders", [])]
        customer = Customer.objects.filter(store=store, pk=data.get("customer") or 0).first()
    except (ValueError, TypeError, KeyError, AttributeError):
        return JsonResponse({"error": "Data transaksi tidak terbaca."}, status=400)
    try:
        sale = pos.sell(register, cashier=request.user, lines=lines, tenders=tenders, customer=customer,
            buyer_name=str(data.get("buyer") or ""), ticket_discount=data.get("discount") or 0,
            note=str(data.get("note") or ""), idem_key=str(data.get("idem") or ""))
    except pos.PosError as error:
        return JsonResponse({"error": str(error)}, status=400)
    from django.urls import reverse
    return JsonResponse({"number": sale.number, "total": float(sale.total), "change": float(sale.change),
        "receipt": reverse("merchant-pos-receipt", args=[sale.pk]),
        "products": pos.catalog(store, register.outlet)})


@login_required(login_url=LOGIN)
def merchant_pos_shift(request, shift_id):
    """One shift: its sales, tenders and cash (the Z report), and the close form while it is open."""
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    shift = get_object_or_404(PosShift.objects.select_related("register__outlet", "register__cash_account",
        "opened_by", "closed_by"), pk=shift_id, register__store=store)
    register = _register(request, store, shift.register_id)
    if request.method == "POST":
        _require_roles(request, store, SHIFT_ROLES)
        try:
            pos.close_shift(shift, counted_cash=bank.parse_amount(request.POST.get("counted_cash") or "") or 0,
                keep_float=bank.parse_amount(request.POST.get("keep_float") or "0") or 0,
                deposit_to=_account(store, request.POST.get("deposit_to")), note=request.POST.get("note", ""),
                actor=request.user)
        except pos.PosError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Shift ditutup.")
        return redirect("merchant-pos-shift", shift.pk)
    drawers = set(PosRegister.objects.filter(store=store).values_list("cash_account_id", flat=True))
    return render(request, "merchant/pos_shift.html", _merchant_context(request, store, shift=shift, register=register,
        summary=pos.shift_summary(shift), sales=shift.sales.select_related("cashier", "invoice").prefetch_related("tenders"),
        accounts=[a for a in bank.bank_accounts(store) if a.id not in drawers],
        can_shift=permissions.can(request.user, store, "pos.shift"),
        can_void=permissions.can(request.user, store, "pos.void"),
        can_refund=permissions.can(request.user, store, "pos.refund"),
        history=audit_trail(store, shift)))


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_paid_out(request, shift_id):
    store = _staff_store(request)
    _require_roles(request, store, SHIFT_ROLES)
    shift = get_object_or_404(PosShift, pk=shift_id, register__store=store)
    try:
        pos.record_paid_out(shift, amount=bank.parse_amount(request.POST.get("amount") or "0") or 0,
            reason=request.POST.get("reason", ""), category=request.POST.get("category", "6900"),
            actor=request.user)
    except pos.PosError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Kas keluar dicatat.")
    return redirect("merchant-pos-shift", shift.pk)


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_paid_in(request, shift_id):
    store = _staff_store(request)
    _require_roles(request, store, roles("pos.refund"))
    shift = get_object_or_404(PosShift, pk=shift_id, register__store=store)
    try:
        pos.record_paid_in(shift, amount=bank.parse_amount(request.POST.get("amount") or "0") or 0,
            fund_from=_account(store, request.POST.get("fund_from")), reason=request.POST.get("reason", ""),
            actor=request.user)
    except pos.PosError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Kas masuk dicatat.")
    return redirect("merchant-pos-shift", shift.pk)


def _receipt_context(store, sale):
    profile = tax_profile(store)
    return {"merchant_store": store, "sale": sale, "invoice": sale.invoice, "register": sale.register,
        "config": pos.settings_for(sale.register.outlet), "profile": profile,
        "tenders": [(dict(PosTender.METHODS)[t.method], t) for t in sale.tenders.all()]}


@login_required(login_url=LOGIN)
def merchant_pos_receipt(request, sale_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    sale = get_object_or_404(PosSale.objects.select_related("invoice", "register__outlet", "cashier"), pk=sale_id,
        store=store)
    _register(request, store, sale.register_id)
    return render(request, "pos/receipt.html", {**_receipt_context(store, sale),
        "auto_print": "print" in request.GET,
        "can_void": permissions.can(request.user, store, "pos.void"),
        "can_refund": permissions.can(request.user, store, "pos.refund")})


@login_required(login_url=LOGIN)
@require_POST
def merchant_pos_void(request, sale_id):
    store = _staff_store(request)
    _require_roles(request, store, roles("pos.void"))
    sale = get_object_or_404(PosSale, pk=sale_id, store=store)
    _register(request, store, sale.register_id)
    try:
        pos.void_sale(sale, actor=request.user, reason=request.POST.get("reason", ""))
    except pos.PosError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Struk {sale.number} dibatalkan.")
    return redirect("merchant-pos-shift", sale.shift_id)


@login_required(login_url=LOGIN)
def merchant_pos_refund(request, sale_id):
    """Retur/refund of a paid receipt: pick quantities off its invoice lines, refunded from the
    register's own drawer."""
    from apps.tax.credit_notes import creditable

    store = _staff_store(request)
    _require_roles(request, store, roles("pos.refund"))
    sale = get_object_or_404(PosSale.objects.select_related("invoice", "register__outlet", "register__cash_account"),
        pk=sale_id, store=store)
    _register(request, store, sale.register_id)
    if request.method == "POST":
        lines = []
        for row in creditable(sale.invoice):
            item = row["item"]
            try:
                quantity = Decimal(request.POST.get(f"qty_{item.pk}") or "0")
            except ArithmeticError:
                quantity = Decimal(0)
            if quantity > 0:
                lines.append({"item": item, "quantity": quantity,
                    "restock": request.POST.get(f"restock_{item.pk}") == "1"})
        try:
            note = pos.refund_sale(sale, actor=request.user, lines=lines, reason=request.POST.get("reason", ""))
        except pos.PosError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"Nota kredit {note.number} dibuat, Rp{note.total:,.0f} dikembalikan dari laci.".replace(",", "."))
            return redirect("merchant-pos-shift", sale.shift_id)
    rows = creditable(sale.invoice)
    return render(request, "merchant/pos_refund.html", _merchant_context(request, store, sale=sale, rows=rows))


# ---------------------------------------------------------------------------
# /kasir/: the outlet's own app. Its own sign-in, its own look, no back-office chrome.
# ---------------------------------------------------------------------------

class PosLoginView(LoginView):
    """The kiosk's sign-in: the same email-or-WhatsApp-number account as the back office, a
    different page, and always landing in the kiosk, never wherever ``?next=`` points (a tablet
    bookmarked to this URL must never be walked into the back office by a stray link)."""
    template_name = "pos/login.html"
    authentication_form = MerchantLoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        return reverse("pos-app")

    def get_default_redirect_url(self):
        return reverse("pos-app")


@login_required(login_url="pos-login")
def kasir_home(request):
    """Pick a register, open or resume its shift, or go straight to selling if one is already open."""
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    registers = list(pos.registers_for(store, request.user))
    open_shifts = {shift.register_id: shift for shift in PosShift.objects.filter(register__in=registers,
        status=PosShift.OPEN)}
    rows = [{"register": register, "shift": open_shifts.get(register.id),
        "balance": pos.drawer_balance(register)} for register in registers]
    drawers = {register.cash_account_id for register in registers}
    return render(request, "pos/home.html", {"merchant_store": store, "request": request, "rows": rows,
        "can_shift": permissions.can(request.user, store, "pos.shift"),
        "accounts": [a for a in bank.bank_accounts(store) if a.id not in drawers]})


@require_POST
def pos_logout(request):
    return sign_out(request, reverse("pos-login"), clear_site_cookies=True)


def public_receipt(request, token):
    """The buyer's e-receipt (the QR on the printed one). The token is the only key."""
    sale = get_object_or_404(PosSale.objects.select_related("invoice", "register__outlet", "cashier", "store"),
        token=token) if token else None
    if sale is None:
        raise Http404
    return render(request, "pos/receipt.html", {**_receipt_context(sale.store, sale), "public": True})
