from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalog.models import Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog
from apps.shops.permissions import roles

from .models import Plan, Subscription, SubscriptionInvoice
from .plans import allowed_outlets, effective_plan, plan_amount, subscription_status
from .renewals import RenewalError, open_payment_page
from .services import (
    BillingError,
    cancel_pending_invoice,
    cancel_subscription,
    check_pending_invoice,
    mark_invoice_paid,
    outlet_purchase_quote,
    reduce_outlets,
    resume_subscription,
    start_checkout,
    start_outlet_purchase,
)

BILLING_ROLES = roles("billing.manage")


def _usage(plan, products, channels, outlets, outlet_limit):
    """The meters on the plan card: used, limit (None = unlimited) and the bar's width in whole percent."""
    def meter(label, used, limit):
        pct = 0 if not limit else min(100, round(used * 100 / limit))
        return {"label": label, "used": used, "limit": limit, "pct": pct, "full": bool(limit) and used >= limit}
    return [meter("Produk aktif", products, getattr(plan, "max_products", None)),
        meter("Toko marketplace", channels, getattr(plan, "max_channels", None)),
        meter("Outlet", outlets, outlet_limit)]


def _settle_recent_payments(store):
    """The buyer comes back from DOKU to this page. DOKU's notification can be late or not set for the payment
    method used, so ask DOKU about the last few open DOKU bills now; a paid one activates the plan."""
    from datetime import timedelta

    from django.utils import timezone

    recent = store.subscription_invoices.filter(status="pending", provider="doku",
        created_at__gte=timezone.now() - timedelta(hours=3)).order_by("-created_at")[:3]
    paid = False
    for invoice in recent:
        paid = check_pending_invoice(invoice) == "paid" or paid
    if paid:
        store.refresh_from_db()   # the lock date moved with the payment


@login_required(login_url="/login/")
def merchant_billing(request):
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    _settle_recent_payments(store)
    subscription = getattr(store, "subscription", None)
    active_outlets = _active_outlets(store)
    return render(request, "merchant/billing.html", _merchant_context(request, store,
        title="Langganan", section="billing", subscription=subscription,
        usage=_usage(effective_plan(store), Product.objects.filter(store=store, is_active=True).count(),
            store.sales_channels.count(), active_outlets, allowed_outlets(store)),
        pending_invoices=store.subscription_invoices.filter(status="pending").select_related("plan"),
        plan=effective_plan(store), plans=Plan.objects.filter(is_public=True),
        active_products=Product.objects.filter(store=store, is_active=True).count(),
        channel_count=store.sales_channels.count(), status=subscription_status(store),
        active_outlets=_active_outlets(store), allowed_outlets=allowed_outlets(store),
        outlet_quote=_outlet_quote(subscription),
        renewal_notice_days=settings.RENEWAL_NOTICE_DAYS,
        invoices=store.subscription_invoices.select_related("plan")[:12],
        ))


@login_required(login_url="/login/")
def merchant_billing_checkout(request):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    plan = get_object_or_404(Plan, code=request.POST.get("plan", ""), is_public=True)
    try:
        months = int(request.POST.get("months", "1"))
        invoice = start_checkout(store, plan, months, request.user)
    except (ValueError, BillingError) as error:
        messages.error(request, str(error) if isinstance(error, BillingError) else "Periode tidak valid.")
        return redirect("merchant-billing")
    return redirect(invoice.checkout_url)


@login_required(login_url="/login/")
def merchant_billing_check(request, invoice_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice, id=invoice_id, store=store)
    status = check_pending_invoice(invoice)
    labels = {"paid": "Pembayaran diterima. Paket Anda sudah aktif.",
        "cancelled": "Tagihan kedaluwarsa tanpa pembayaran."}
    messages.info(request, labels.get(status, "Pembayaran belum diterima. Coba cek lagi nanti."))
    return redirect("merchant-billing")


@login_required(login_url="/login/")
def billing_demo_pay(request, invoice_id):
    """Local stand-in for DOKU, only while PAYMENT_PROVIDER=demo: a demo invoice left over
    from before DOKU was configured must not activate a paid plan for free."""
    if settings.PAYMENT_PROVIDER != "demo" or not settings.ALLOW_DEMO_PAYMENTS:
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice.objects.select_related("plan"), id=invoice_id,
        store=store, provider="demo")
    if request.method == "POST":
        if invoice.status == "pending":
            mark_invoice_paid(invoice.id, amount=invoice.amount)
            messages.success(request, f"Paket {invoice.plan.name} aktif.")
        return redirect("merchant-billing")
    return render(request, "merchant/billing_demo_pay.html", _merchant_context(request, store,
        title="Pembayaran demo", invoice=invoice))


@login_required(login_url="/login/")
def merchant_billing_pay(request, invoice_id):
    """Open a fresh payment page for a pending bill (renewal or not)."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice, id=invoice_id, store=store)
    try:
        url = open_payment_page(invoice, request.user)
    except RenewalError as error:
        messages.error(request, str(error))
        return redirect("merchant-billing")
    return redirect(url)


@login_required(login_url="/login/")
def merchant_billing_autorenew(request):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    subscription = get_object_or_404(Subscription, store=store)
    months = 12 if request.POST.get("renew_months") == "12" else 1
    subscription.auto_renew = request.POST.get("auto_renew") == "on"
    subscription.renew_months = months
    subscription.save(update_fields=["auto_renew", "renew_months", "updated_at"])
    renewal = store.subscription_invoices.filter(kind="renewal", status="pending").first()
    # An open bill follows the new choice unless a payment page for it may still be paid.
    if renewal and not renewal.checkouts.exists():
        if not subscription.auto_renew:
            renewal.status = "cancelled"
            renewal.save(update_fields=["status"])
        elif renewal.months != months:
            renewal.months = months
            renewal.amount = plan_amount(renewal.plan, months, renewal.extra_outlets)
            renewal.save(update_fields=["months", "amount"])
    AuditLog.objects.create(store=store, actor=request.user, action="billing.autorenew_updated",
        object_type="Subscription", object_id=str(subscription.id),
        details={"auto_renew": subscription.auto_renew, "months": months})
    messages.success(request, "Perpanjangan otomatis aktif." if subscription.auto_renew else
        "Perpanjangan otomatis dimatikan. Paket berakhir di akhir periode.")
    return redirect("merchant-billing")


def _active_outlets(store):
    return store.warehouses.filter(is_outlet=True, is_active=True).count()


def _outlet_quote(subscription):
    """The price of one more outlet for the rest of this period, for the billing page."""
    try:
        return outlet_purchase_quote(subscription, 1) if subscription else None
    except BillingError:
        return None


@login_required(login_url="/login/")
def merchant_billing_outlets(request):
    """Add outlets (paid now for the rest of the period) or reduce them from now on."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    try:
        count = max(0, int(request.POST.get("count", "0")))
    except ValueError:
        count = 0
    try:
        if request.POST.get("action") == "reduce":
            reduce_outlets(store, count, _active_outlets(store))
            messages.success(request, f"Outlet tambahan menjadi {count}. Berlaku mulai tagihan berikutnya.")
            return redirect("merchant-billing")
        invoice = start_outlet_purchase(store, count, request.user)
        return redirect(open_payment_page(invoice, request.user))
    except (BillingError, RenewalError) as error:
        messages.error(request, str(error))
        return redirect("merchant-billing")


@login_required(login_url="/login/")
def merchant_billing_review(request):
    """The page before payment: what is chosen, what it includes, what it costs, from when to when."""
    from datetime import timedelta

    from django.utils import timezone

    from .plans import PERIOD_DAYS
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    plan = get_object_or_404(Plan, code=request.GET.get("plan", ""), is_public=True)
    months = 12 if request.GET.get("months") == "12" else 1
    if not plan.is_paid or plan.contact_sales or plan.price_for(months) <= 0:
        messages.error(request, "Paket atau periode tidak tersedia.")
        return redirect("merchant-billing")
    current = getattr(store, "subscription", None)
    extra = current.extra_outlets if current and current.plan_id == plan.id else 0
    amount = plan_amount(plan, months, extra)
    now = timezone.now()
    renewing = bool(current and current.plan_id == plan.id and current.current_period_end
        and current.current_period_end > now)
    start = current.current_period_end if renewing else now
    end = start + timedelta(days=PERIOD_DAYS[months])
    slashed = plan.list_price_monthly if months == 1 else plan.list_price_yearly
    pct = plan.discount_pct_monthly if months == 1 else plan.discount_pct_yearly
    saving = (slashed - plan.price_for(months)) if pct else 0
    other = 1 if months == 12 else 12
    other_option = {"months": other, "price": plan.price_for(other),
        "pct": plan.discount_pct_monthly if other == 1 else plan.discount_pct_yearly} if plan.price_for(other) > 0 else None
    return render(request, "merchant/billing_review.html", _merchant_context(request, store,
        title="Tinjau langganan", section="billing", plan=plan, months=months, amount=amount, extra=extra,
        plan_price=plan.price_for(months), slashed=slashed if pct else 0, pct=pct, saving=saving,
        renewing=renewing, start=start, end=end, other_option=other_option, current=current,
        changing=bool(current and current.plan_id != plan.id and current.plan.is_paid
            and current.current_period_end and current.current_period_end > now),
        sales_contact_url=_sales_url()))


def _sales_url():
    from apps.shops.trial import sales_contact
    return sales_contact()["whatsapp_url"]


@login_required(login_url="/login/")
def merchant_billing_cancel(request):
    """Batalkan langganan (stop renewing; active until the paid period ends) or turn renewing back on."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    try:
        if request.POST.get("action") == "resume":
            resume_subscription(store, request.user)
            messages.success(request, "Perpanjangan otomatis aktif kembali.")
        else:
            subscription = cancel_subscription(store, request.user)
            messages.success(request, "Langganan dibatalkan. Paket tetap aktif sampai "
                f"{subscription.current_period_end:%d %b %Y} dan tidak akan ditagih lagi.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect("merchant-billing")


@login_required(login_url="/login/")
def merchant_billing_cancel_invoice(request, invoice_id):
    """Batalkan tagihan: drop a bill that has not been paid."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice, id=invoice_id, store=store)
    try:
        cancel_pending_invoice(invoice, request.user)
        messages.success(request, "Tagihan dibatalkan.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect("merchant-billing")


@login_required(login_url="/login/")
def merchant_billing_payment(request, invoice_id):
    """Our own payment page: the bill, a choice of bank, then the virtual account number to pay."""
    from . import payment_page as pay

    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice.objects.select_related("plan"), id=invoice_id, store=store)
    if request.method == "POST":
        try:
            if request.POST.get("method") == "lainnya":
                return redirect(pay.open_doku_checkout(invoice, request.user))
            pay.open_virtual_account(invoice, request.POST.get("bank", ""), request.user)
        except pay.PaymentPageError as error:
            messages.error(request, str(error))
        return redirect("merchant-billing-payment", invoice.id)
    if invoice.status == "pending" and invoice.provider == "doku":
        check_pending_invoice(invoice)
        invoice.refresh_from_db()
    code = pay.open_code(invoice) if invoice.status == "pending" else None
    return render(request, "merchant/billing_payment.html", _merchant_context(request, store,
        title="Pembayaran langganan", invoice=invoice, code=code, banks=pay.banks(),
        bank_name=pay.BANKS.get(code.method.removeprefix("va_"), "") if code else "",
        steps=pay.steps_for(code.method) if code else [],
        demo=settings.PAYMENT_PROVIDER == "demo" and settings.ALLOW_DEMO_PAYMENTS))


@login_required(login_url="/login/")
def merchant_billing_payment_status(request, invoice_id):
    """What the payment page polls. DOKU is asked at most every 15 seconds per bill."""
    from django.core.cache import cache
    from django.http import JsonResponse

    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    invoice = get_object_or_404(SubscriptionInvoice, id=invoice_id, store=store)
    status = invoice.status
    if status == "pending" and invoice.provider == "doku" and cache.add(f"billing-poll:{invoice.id}", 1, 15):
        status = check_pending_invoice(invoice)
    return JsonResponse({"status": status})
