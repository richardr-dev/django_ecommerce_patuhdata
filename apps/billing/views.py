from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalog.models import Product
from apps.orders.views import _merchant_context, _require_roles, _staff_store

from .models import Plan, SubscriptionInvoice
from .plans import effective_plan
from .services import BillingError, check_pending_invoice, mark_invoice_paid, start_checkout

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
        channel_count=store.sales_channels.count(),
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
