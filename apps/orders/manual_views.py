"""Back office: create a manual order and send its payment link. Shopper: open the payment."""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.catalog.models import Product, ProductVariant
from apps.integrations.http import ProviderError

from .access import get_customer_order_or_404, remember_customer_order
from .manual_orders import (
    ManualOrderError,
    ManualOrderForm,
    create_manual_order,
    parse_lines,
    payment_page,
    start_payment,
    whatsapp_message,
    whatsapp_url,
)
from .models import Order

logger = logging.getLogger(__name__)
ORDER_ROLES = {"admin", "manager", "ops", "customer_service"}


def _sku_options(store):
    """SKU suggestions for the item rows (the field still accepts any SKU typed)."""
    variants = ProductVariant.objects.filter(product__store=store, is_active=True,
        product__is_active=True).select_related("product").order_by("product__name", "id")[:400]
    products = Product.objects.filter(store=store, is_active=True).exclude(
        variants__is_active=True).order_by("name")[:400]
    options = [{"sku": product.sku, "label": product.name, "price": product.price}
        for product in products]
    options += [{"sku": variant.sku, "label": f"{variant.product.name} · {variant.name}",
        "price": variant.effective_price} for variant in variants]
    return sorted(options, key=lambda option: option["label"].lower())


@login_required(login_url="/merchant/login/")
def merchant_order_new(request):
    from .views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, ORDER_ROLES)
    form = ManualOrderForm(request.POST or None, store=store)
    rows, line_errors = (parse_lines(request.POST) if request.method == "POST" else ([], []))
    if request.method == "POST" and form.is_valid() and not line_errors:
        try:
            order = create_manual_order(store, request.user, form.cleaned_data, rows)
        except ManualOrderError as error:
            line_errors = [str(error)]
        else:
            messages.success(request, "Pesanan dibuat dan stok sudah disisihkan. Kirim link "
                "pembayarannya ke pelanggan.")
            return redirect("merchant-order-payment-link", order_id=order.id)
    posted_rows = list(zip(request.POST.getlist("sku"), request.POST.getlist("quantity"),
        request.POST.getlist("price"), strict=False)) if request.method == "POST" else []
    return render(request, "merchant/order_new.html", _merchant_context(request, store,
        title="Buat pesanan", section="orders", form=form, line_errors=line_errors,
        posted_rows=posted_rows or [("", "1", "")], sku_options=_sku_options(store)))


@login_required(login_url="/merchant/login/")
def merchant_order_payment_link(request, order_id):
    from .views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, ORDER_ROLES)
    order = get_object_or_404(Order.objects.prefetch_related("items"), id=order_id, store=store)
    return render(request, "merchant/order_payment_link.html", _merchant_context(request, store,
        title="Link pembayaran", section="orders", order=order, link=payment_page(order),
        whatsapp_url=whatsapp_url(order), message=whatsapp_message(order)))


def order_pay(request, order_id):
    """The shopper's "Bayar sekarang" on an order that has no payment page yet."""
    if not request.store or request.method != "POST":
        raise Http404
    order = get_customer_order_or_404(request, order_id)
    remember_customer_order(request, order)
    back = reverse("order-detail", args=[order.id])
    if order.status != "pending":
        return redirect(back)
    try:
        url = start_payment(order)
    except ProviderError:
        logger.exception("payment session failed for manual order %s", order.id)
        messages.error(request, "Halaman pembayaran belum dapat dibuka. Coba lagi beberapa saat, "
            "atau hubungi toko.")
        return redirect(back)
    return redirect(url)
