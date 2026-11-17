"""Kasir (POS): registers, shifts and receipts. See docs/pos-architecture.md.

The register never writes the ledger itself. A receipt is ``create_sales_invoice(channel="pos",
warehouse=outlet)`` (stock off that outlet's shelf, HPP, the invoice entry) plus one invoice payment per
tender: cash into the register's own cash account, card and QRIS into Piutang EDC / QRIS (1210), a
transfer into the bank account named. What the drawer should hold is therefore simply the ledger
balance of the register's cash account, and closing a shift compares that with the count."""
import secrets
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.accounting import bank
from apps.accounting.ledger import (
    CASH_OVER,
    CASH_SHORT,
    LedgerError,
    post_entry,
    register_backlog,
    unposted,
)
from apps.accounting.models import CashTransaction
from apps.orders.models import Order, OrderStatusEvent
from apps.shops import permissions
from apps.shops.audit import log as audit_log
from apps.tax.models import InvoicePayment
from apps.tax.sales import create_sales_invoice, line_net, price_lines
from apps.tax.services import InvoiceError, exclusive_price, record_payment, tax_profile

from .models import PosOutletSettings, PosRegister, PosSale, PosShift, PosTender

ZERO = Decimal(0)
WALK_IN = "Pelanggan umum"
PAYMENT_METHOD = {PosTender.CASH: InvoicePayment.METHOD_CASH, PosTender.QRIS: InvoicePayment.METHOD_QRIS_POS,
    PosTender.EDC: InvoicePayment.METHOD_EDC, PosTender.TRANSFER: InvoicePayment.METHOD_TRANSFER}


class PosError(ValueError):
    pass


def rp(value):
    return "Rp" + f"{Decimal(value):,.0f}".replace(",", ".")


def _money(value):
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except ArithmeticError as exc:
        raise PosError("Jumlah tidak dikenali.") from exc


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------

def settings_for(outlet):
    return PosOutletSettings.objects.get_or_create(outlet=outlet)[0]


def update_settings(outlet, *, prices_include_tax, rounding_step, max_discount_pct, receipt_header="",
        receipt_footer="", actor=None):
    """How an outlet sells: PPN-inclusive shelf prices, cash rounding, the cashier's discount limit and
    the text on its receipts."""
    try:
        rounding_step, limit = int(rounding_step or 0), Decimal(str(max_discount_pct or 0))
    except (ValueError, ArithmeticError) as exc:
        raise PosError("Angka pengaturan tidak dikenali.") from exc
    if rounding_step not in dict(PosOutletSettings.ROUNDING):
        raise PosError("Pilih pembulatan yang tersedia.")
    if not 0 <= limit <= 100:
        raise PosError("Batas diskon kasir antara 0 dan 100 persen.")
    config = settings_for(outlet)
    config.prices_include_tax, config.rounding_step, config.max_discount_pct = bool(prices_include_tax), rounding_step, limit
    config.receipt_header, config.receipt_footer = (receipt_header or "").strip()[:200], (receipt_footer or "").strip()[:200]
    config.save()
    audit_log(outlet.store, actor, "pos.settings_changed", config, outlet=outlet.name, pembulatan=rounding_step,
        batas_diskon=limit, harga_termasuk_ppn=config.prices_include_tax)
    return config


@transaction.atomic
def create_register(store, *, outlet, code, name="", actor=None):
    """A new cash drawer in ``outlet``, with its own cash account ("Kas kasir <outlet> <code>")."""
    code = (code or "").strip().upper()[:8]
    if not code:
        raise PosError("Kode register wajib diisi, misalnya K1.")
    if outlet is None or outlet.store_id != store.id or not outlet.is_active:
        raise PosError("Pilih outlet aktif milik perusahaan ini.")
    if not outlet.is_outlet:
        raise PosError(f"{outlet.name} adalah gudang, bukan outlet. Tandai sebagai outlet lebih dulu.")
    if PosRegister.objects.filter(store=store, code__iexact=code).exists():
        raise PosError(f"Register {code} sudah ada.")
    try:
        cash = bank.add_bank_account(store, name=f"Kas kasir {outlet.name} {code}", kind="cash")
    except bank.BankError as exc:
        raise PosError(str(exc)) from exc
    register = PosRegister.objects.create(store=store, outlet=outlet, code=code,
        name=(name or "").strip()[:80] or f"Kasir {code}", cash_account=cash)
    settings_for(outlet)
    audit_log(store, actor, "pos.register_created", register, outlet=outlet.name, code=code)
    return register


def registers_for(store, user):
    """The active registers ``user`` may serve: every outlet for back-office roles, only the pinned
    outlet for a member with ``StoreStaff.outlet`` set (a cashier)."""
    registers = PosRegister.objects.filter(store=store, is_active=True).select_related("outlet", "cash_account")
    member = store.staff_members.filter(user=user).first()
    if member and member.outlet_id:
        registers = registers.filter(outlet_id=member.outlet_id)
    return registers


def drawer_balance(register):
    """What the books say is in the drawer now."""
    return bank.balances(register.store, [register.cash_account])[register.cash_account.id]


def open_shift_of(register, *, lock=False):
    shifts = PosShift.objects.filter(register=register, status=PosShift.OPEN)
    return (shifts.select_for_update() if lock else shifts).first()


# ---------------------------------------------------------------------------
# Shifts
# ---------------------------------------------------------------------------

def _transfer(store, source, target, amount, text, actor):
    try:
        return bank.record_cash_transaction(store, kind=CashTransaction.TRANSFER, bank_account=source,
            to_bank_account=target, amount=amount, date=timezone.localdate(), description=text, actor=actor)
    except (bank.BankError, LedgerError) as exc:
        raise PosError(str(exc)) from exc


@transaction.atomic
def open_shift(register, *, opening_float, actor, fund_from=None):
    """Start a shift with the counted float. The count must agree with the books: a difference is
    moved from (or back to) ``fund_from``, the account the float money comes from, and refused when
    none is named, so a drawer never starts with money nobody can explain."""
    register = PosRegister.objects.select_for_update().get(pk=register.pk)
    if not register.is_active:
        raise PosError("Register ini tidak aktif.")
    if open_shift_of(register):
        raise PosError(f"{register.name} masih punya shift yang terbuka. Tutup shift itu lebih dulu.")
    opening_float = _money(opening_float)
    if opening_float < 0:
        raise PosError("Modal kasir tidak boleh negatif.")
    difference = opening_float - drawer_balance(register)
    if difference:
        if fund_from is None:
            raise PosError(f"Menurut buku laci berisi {rp(opening_float - difference)}, Anda menghitung "
                f"{rp(opening_float)}. Pilih rekening asal modal kasir, atau hitung ulang.")
        if fund_from.pk == register.cash_account_id or fund_from.store_id != register.store_id:
            raise PosError("Pilih rekening lain sebagai asal modal kasir.")
        if difference > 0:
            _transfer(register.store, fund_from, register.cash_account, difference,
                f"Modal kasir {register.name}", actor)
        else:
            _transfer(register.store, register.cash_account, fund_from, -difference,
                f"Kelebihan laci {register.name} disetor", actor)
    shift = PosShift.objects.create(register=register, opened_by=actor, opening_float=opening_float)
    audit_log(register.store, actor, "pos.shift_opened", shift, register=register.code, modal=opening_float)
    return shift


def post_shift_variance(shift, *, strict=False, actor=None):
    """Book the drawer's shortage (6980) or surplus (7250) found at the close. Idempotent."""
    if not shift.variance:
        return None
    register = shift.register
    other = CASH_SHORT if shift.variance < 0 else CASH_OVER
    return post_entry(register.store, date=shift.closed_on or timezone.localdate(), strict=strict, actor=actor,
        cost_center=register.outlet.cost_center, source=("pos_shift", shift.pk),
        memo=f"Selisih kas tutup shift {register.outlet.name} {register.code}",
        amounts={register.cash_account.account.code: shift.variance, other: -shift.variance})


@transaction.atomic
def close_shift(shift, *, counted_cash, actor, deposit_to=None, keep_float=0, note=""):
    """Close with the counted cash. The difference from the books is posted as a shortage or surplus,
    then everything above ``keep_float`` (tomorrow's float) goes to ``deposit_to`` when given."""
    shift = PosShift.objects.select_for_update().select_related("register__outlet", "register__cash_account__account",
        "register__store").get(pk=shift.pk)
    if not shift.is_open:
        raise PosError("Shift ini sudah ditutup.")
    register, today = shift.register, timezone.localdate()
    counted, keep_float = _money(counted_cash), _money(keep_float)
    if counted < 0 or keep_float < 0:
        raise PosError("Jumlah tidak boleh negatif.")
    if keep_float > counted:
        raise PosError("Modal yang ditinggal melebihi uang yang dihitung.")
    expected = drawer_balance(register)
    shift.expected_cash, shift.counted_cash, shift.variance = expected, counted, counted - expected
    shift.status, shift.closed_by, shift.closed_at, shift.closed_on = PosShift.CLOSED, actor, timezone.now(), today
    shift.note = (note or "").strip()[:300]
    deposit = counted - keep_float if deposit_to is not None else ZERO
    shift.deposited = deposit
    shift.save()
    try:
        post_shift_variance(shift, strict=True, actor=actor)
    except LedgerError as exc:
        raise PosError(str(exc)) from exc
    if deposit > 0:
        if deposit_to.pk == register.cash_account_id or deposit_to.store_id != register.store_id:
            raise PosError("Pilih rekening lain sebagai tujuan setoran.")
        _transfer(register.store, register.cash_account, deposit_to, deposit,
            f"Setoran kas {register.name}", actor)
    audit_log(register.store, actor, "pos.shift_closed", shift, register=register.code, dihitung=counted,
        menurut_buku=expected, selisih=shift.variance, disetor=deposit)
    return shift


@register_backlog
def post_unposted_shift_variances():
    posted = 0
    shifts = PosShift.objects.filter(status=PosShift.CLOSED).exclude(variance=0)
    for shift in unposted(shifts, "pos_shift"):
        post_shift_variance(shift)
        posted += 1
    return posted


def catalog(store, outlet):
    """What the register can sell: active products without variants, with the shelf price and what this
    outlet holds (``None``: not stock-tracked, e.g. a service)."""
    from apps.catalog.models import InventoryLevel, Product
    here = {level.product_id: level.available for level in InventoryLevel.objects.filter(warehouse=outlet)}
    tracked = set(InventoryLevel.objects.filter(warehouse__store=store).values_list("product_id", flat=True))
    products = Product.objects.filter(store=store, is_active=True, variants__isnull=True).select_related("category")
    return [{"id": product.id, "name": product.name, "sku": product.sku, "price": float(product.price),
        "category": product.category.name if product.category_id else "",
        "stock": here.get(product.id, 0) if product.id in tracked else None}
        for product in products.order_by("name")]


def shift_summary(shift):
    """Figures for the shift page and the Z report."""
    sales = shift.sales.filter(status=PosSale.PAID)
    totals = sales.aggregate(count=Count("pk"), total=Sum("total", default=ZERO))
    labels = dict(PosTender.METHODS)
    by_method = {row["method"]: row for row in PosTender.objects.filter(sale__in=sales).values("method").annotate(
        amount=Sum("amount", default=ZERO), count=Count("pk"))}
    tenders = [{"method": code, "label": labels[code], "amount": by_method.get(code, {}).get("amount", ZERO),
        "count": by_method.get(code, {}).get("count", 0)} for code, _label in PosTender.METHODS]
    expected = drawer_balance(shift.register) if shift.is_open else shift.expected_cash
    return {"count": totals["count"], "total": totals["total"], "tenders": tenders,
        "cash_sales": by_method.get(PosTender.CASH, {}).get("amount", ZERO), "opening_float": shift.opening_float,
        "expected_cash": expected, "voided": shift.sales.filter(status=PosSale.VOID).count()}


# ---------------------------------------------------------------------------
# Selling
# ---------------------------------------------------------------------------

def _round_payable(amount, step):
    """What the buyer pays: whole rupiah, or rounded down to Rp100 / Rp500 in the buyer's favour."""
    if step and step > 1:
        return (amount / step).to_integral_value(ROUND_FLOOR) * step
    return amount.quantize(Decimal(1), ROUND_HALF_UP)


def _clean_cart(store, lines):
    cart = []
    for line in lines:
        product = line.get("product")
        if product is None or product.store_id != store.id:
            raise PosError("Produk tidak ditemukan.")
        if product.variants.exists():
            raise PosError(f"{product.name} punya varian; varian belum bisa dijual di kasir.")
        try:
            qty = Decimal(str(line.get("qty") or 0))
        except ArithmeticError as exc:
            raise PosError("Jumlah tidak dikenali.") from exc
        if qty <= 0 or qty != qty.to_integral_value():
            raise PosError(f"Jumlah {product.name} harus bilangan bulat lebih dari nol.")
        price = _money(line["unit_price"]) if line.get("unit_price") not in (None, "") else _money(product.price)
        discount = _money(line.get("discount"))
        if price < 0 or discount < 0 or discount > qty * price:
            raise PosError(f"Harga atau diskon {product.name} tidak wajar.")
        cart.append({"product": product, "qty": qty, "unit_price": price, "discount": discount})
    if not cart:
        raise PosError("Keranjang masih kosong.")
    return cart


def _check_authority(store, cashier, cart, ticket_discount, limit_pct):
    """A cashier sells at the shelf price and discounts up to the outlet's limit; beyond that it takes
    someone holding ``pos.discount_override``."""
    if permissions.can(cashier, store, "pos.discount_override"):
        return
    for line in cart:
        if line["unit_price"] != _money(line["product"].price):
            raise PosError(f"Harga {line['product'].name} berbeda dari harga jual. Minta manajer mengubahnya.")
    gross = sum((line["qty"] * line["unit_price"] for line in cart), ZERO)
    given = sum((line["discount"] for line in cart), ZERO) + ticket_discount
    if gross and given * 100 > gross * Decimal(limit_pct):
        raise PosError(f"Diskon melebihi batas kasir ({limit_pct:.0f}%). Minta persetujuan manajer.")


def quote(store, outlet, cart, ticket_discount=0):
    """What a cart comes to: the invoice lines and discount (before PPN), the invoice total, what the
    buyer pays after rounding, and the rounding to book."""
    config = settings_for(outlet)
    ticket_discount = _money(ticket_discount)
    inclusive = bool(config.prices_include_tax and tax_profile(store).is_pkp and store.tax_rate)
    shelf_total = sum((line["qty"] * line["unit_price"] - line["discount"] for line in cart), ZERO)
    if ticket_discount > shelf_total:
        raise PosError("Diskon melebihi nilai belanja.")
    convert = (lambda value: exclusive_price(store, value)) if inclusive else _money
    lines = [{"product": line["product"], "qty": line["qty"], "unit_price": convert(line["unit_price"]),
        "discount": convert(line["discount"])} for line in cart]
    discount = convert(ticket_discount)
    _subtotal, tax, invoice_total = price_lines(lines, discount, 0, store=store)
    payable = _round_payable(shelf_total - ticket_discount if inclusive else invoice_total, config.rounding_step)
    return {"lines": lines, "discount": discount, "tax": tax, "invoice_total": invoice_total, "payable": payable,
        "rounding": payable - invoice_total, "inclusive": inclusive, "shelf_total": shelf_total,
        "net": sum((line_net(line) for line in lines), ZERO) - discount}


def _clean_tenders(store, tenders, payable):
    cleaned, non_cash = [], ZERO
    for tender in tenders:
        method, amount = tender.get("method"), _money(tender.get("amount"))
        if method not in PAYMENT_METHOD:
            raise PosError("Cara bayar tidak dikenal.")
        if amount <= 0:
            continue
        account = tender.get("bank_account")
        if method == PosTender.TRANSFER and (account is None or account.store_id != store.id):
            raise PosError("Pilih rekening bank yang menerima transfer.")
        if method != PosTender.CASH:
            non_cash += amount
        cleaned.append({"method": method, "amount": amount, "reference": (tender.get("reference") or "").strip()[:120],
            "bank_account": account if method == PosTender.TRANSFER else None})
    tendered = sum((t["amount"] for t in cleaned), ZERO)
    if non_cash > payable:
        raise PosError("Pembayaran non-tunai melebihi total belanja.")
    if tendered < payable:
        raise PosError(f"Pembayaran kurang {rp(payable - tendered)}.")
    return cleaned, tendered


def _next_number(register, today):
    """``KMG-K1-20261117-0042``: outlet, register, day, running number. The caller holds the shift's
    row lock, so two tablets on one register cannot draw the same number."""
    prefix = f"{register.outlet.code}-{register.code}-{today:%Y%m%d}-"
    last = PosSale.objects.filter(store=register.store, number__startswith=prefix).order_by("-number").first()
    return f"{prefix}{(int(last.number.rsplit('-', 1)[1]) if last else 0) + 1:04d}"


@transaction.atomic
def sell(register, *, cashier, lines, tenders, customer=None, buyer_name="", ticket_discount=0, note="",
        idem_key=""):
    """Ring up one receipt: the invoice (stock off this outlet, HPP, PPN), one payment per tender, and
    the receipt itself. ``lines`` is ``[{"product", "qty", "unit_price"?, "discount"?}]`` at shelf
    prices; ``tenders`` is ``[{"method", "amount", "reference"?, "bank_account"?}]`` with the cash
    amount as handed over (change is worked out here). Returns the ``PosSale``; the same ``idem_key``
    returns the first sale again."""
    store, idem_key = register.store, (idem_key or "").strip()[:64]
    if idem_key:
        earlier = PosSale.objects.filter(store=store, idem_key=idem_key).first()
        if earlier:
            return earlier
    shift = open_shift_of(register, lock=True)
    if shift is None:
        raise PosError("Buka shift kasir lebih dulu.")
    if not permissions.can(cashier, store, "pos.sell"):
        raise PosError("Anda tidak punya hak menjual di kasir.")
    outlet, today = register.outlet, timezone.localdate()
    cart = _clean_cart(store, lines)
    ticket_discount = _money(ticket_discount)
    _check_authority(store, cashier, cart, ticket_discount, settings_for(outlet).max_discount_pct)
    figures = quote(store, outlet, cart, ticket_discount)
    payable = figures["payable"]
    if payable <= 0:
        raise PosError("Total belanja harus lebih dari nol.")
    cleaned, tendered = _clean_tenders(store, tenders, payable)
    name = (buyer_name or "").strip() or (customer.name if customer else WALK_IN)
    try:
        invoice = create_sales_invoice(store, lines=figures["lines"], buyer_name=name, issued_on=today,
            channel="pos", customer=customer, discount=figures["discount"], on_credit=True, due_date=today,
            actor=cashier, warehouse=outlet, number_series="POS", rounding=figures["rounding"],
            cost_center=outlet.cost_center, notes=(note or "").strip()[:300])
        change = tendered - payable
        sale = PosSale.objects.create(store=store, register=register, shift=shift, invoice=invoice, cashier=cashier,
            customer=customer, number=_next_number(register, today), total=payable, tendered=tendered,
            change=change, idem_key=idem_key, token=secrets.token_urlsafe(18), discount=ticket_discount,
            cart=[{"name": line["product"].name, "sku": line["product"].sku, "qty": int(line["qty"]),
                "price": str(line["unit_price"]), "discount": str(line["discount"]),
                "total": str(line["qty"] * line["unit_price"] - line["discount"])} for line in cart])
        for tender in cleaned:
            applied = tender["amount"]
            if tender["method"] == PosTender.CASH and change:
                given = min(change, applied)
                applied, change = applied - given, change - given
            payment = None
            if applied > 0:
                account = register.cash_account if tender["method"] == PosTender.CASH else tender["bank_account"]
                payment = record_payment(invoice, applied, today, PAYMENT_METHOD[tender["method"]],
                    reference=tender["reference"], note=f"Struk {sale.number}", user=cashier, bank_account=account)
            PosTender.objects.create(sale=sale, method=tender["method"], amount=applied, tendered=tender["amount"],
                reference=tender["reference"], payment=payment)
    except InvoiceError as exc:
        raise PosError(str(exc)) from exc
    invoice.refresh_from_db()  # the payments settled it
    # Handed over at the counter: nothing to pack or ship, so it never enters the Pesanan work queues.
    Order.objects.filter(pk=invoice.order_id).update(status="completed")
    OrderStatusEvent.objects.create(order_id=invoice.order_id, status="completed",
        note=f"Diserahkan di kasir {register.code} · struk {sale.number}")
    invoice.order.status = "completed"
    audit_log(store, cashier, "pos.sale", sale, struk=sale.number, total=payable, register=register.code)
    return sale
