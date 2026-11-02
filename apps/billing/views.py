from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalog.models import Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .models import Plan, Subscription, SubscriptionInvoice
from .plans import allowed_outlets, effective_plan, plan_amount, subscription_status
from .renewals import RenewalError, open_payment_page
from .services import (
    BillingError,
    check_pending_invoice,
    mark_invoice_paid,
    outlet_purchase_quote,
    reduce_outlets,
    start_checkout,
    start_outlet_purchase,
)

BILLING_ROLES = {"admin", "finance"}


@login_required(login_url="/merchant/login/")
def merchant_billing(request):
    store = _staff_store(request)
    _require_roles(request, store, BILLING_ROLES)
    subscription = getattr(store, "subscription", None)
    return render(request, "merchant/billing.html", _merchant_context(request, store,
        title="Langganan", section="billing", subscription=subscription,
        plan=effective_plan(store), plans=Plan.objects.filter(is_public=True),
        active_products=Product.objects.filter(store=store, is_active=True).count(),
        channel_count=store.sales_channels.count(), status=subscription_status(store),
        active_outlets=_active_outlets(store), allowed_outlets=allowed_outlets(store),
        outlet_quote=_outlet_quote(subscription),
        renewal_notice_days=settings.RENEWAL_NOTICE_DAYS,
        invoices=store.subscription_invoices.select_related("plan")[:12]))


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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


@login_required(login_url="/merchant/login/")
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
