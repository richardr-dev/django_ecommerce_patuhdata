import csv
import hashlib
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models, transaction
from django.db.models import Count, F, Min, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme

from apps.catalog.models import (
    Category,
    FlashSaleProduct,
    InventoryLevel,
    Product,
    ProductImage,
    ProductVariant,
    Warehouse,
    annotate_available_stock,
)
from apps.catalog.views import paginate
from apps.integrations.http import ProviderError
from apps.integrations.models import OutboxEvent
from apps.integrations.payments import get_payment_provider, reserved_reference
from apps.integrations.shipping import BiteshipProvider, RajaOngkirProvider
from apps.shops.models import AuditLog, Customer, MarketingCampaign, Store, StorefrontSlide
from apps.shops.storefront_templates import STOREFRONT_TEMPLATES, apply_storefront_template

from .access import get_customer_order_or_404, remember_customer_order
from .cart import get_cart
from .engagement import commerce_session, engagement_summary, record_event, update_cart_snapshot
from .forms import (
    CategoryForm,
    CouponForm,
    CustomerRegistrationForm,
    FlashSaleProductForm,
    InventoryForm,
    MarketingCampaignForm,
    ProductForm,
    ProductImageForm,
    ShippingMethodForm,
    StorefrontSlideForm,
    StoreSettingsForm,
    VariantForm,
    WarehouseForm,
)
from .inventory import record_level_reservation, record_movement, reserve_variant
from .loyalty import loyalty_account, redeem_for_order, redemption_quote
from .models import (
    CommerceSession,
    Coupon,
    InventoryAllocation,
    LoyaltyAccount,
    Order,
    OrderItem,
    OrderStatusEvent,
    PaymentTransaction,
    Refund,
    ReturnRequest,
    ShippingMethod,
)
from .pricing import calculate_price
from .refunds import RefundError, create_refund_request, execute_refund, record_manual_refund
from .sales import active_sale_offers, offer_sold_quantity
from .services import (
    InvalidOrderTransition,
    allowed_order_transitions,
    get_coupon,
    normalize_indonesian_phone,
    transition_order,
)
from .shipments import ShipmentBookingError, book_shipment, can_book_shipment, is_booking_pending
from .tracking import sync_order_tracking

# How long a new order may hold stock while its gateway session is being created.
PAYMENT_SESSION_CREATION_WINDOW = timedelta(minutes=15)


def _abandon_unpaid_order(order, coupon):
    """Undo a reservation whose payment session could not be created."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(id=order.id)
        if locked.status != "pending":
            return
        transition_order(locked, "cancelled", note="Sesi pembayaran gagal dibuat")
        if coupon:
            Coupon.objects.filter(id=coupon.id, used_count__gt=0).update(used_count=F("used_count") - 1)


def _cart_signature(lines):
    value = "|".join(
        f'{line["key"]}:{line["quantity"]}:{line["variant"].weight_grams if line["variant"] and line["variant"].weight_grams is not None else line["product"].weight_grams}'
        for line in lines
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _shipping_provider():
    if settings.SHIPPING_RATE_PROVIDER == "rajaongkir": return RajaOngkirProvider()
    return BiteshipProvider()


def _shipping_provider_enabled():
    if settings.SHIPPING_RATE_PROVIDER == "rajaongkir":
        return bool(settings.RAJAONGKIR_API_KEY and
            (settings.RAJAONGKIR_ORIGIN_ID or settings.RAJAONGKIR_ORIGIN_POSTAL_CODE))
    key = settings.BITESHIP_API_KEY
    return bool(key and settings.BITESHIP_ORIGIN_POSTAL_CODE and
        (not key.startswith("biteship_live.") or settings.BITESHIP_ALLOW_LIVE))


def _shipping_provider_name():
    return "RajaOngkir" if settings.SHIPPING_RATE_PROVIDER == "rajaongkir" else "Biteship"


def shipping_areas(request):
    if not request.store: raise Http404
    query = request.GET.get("q", "").strip()
    if len(query) < 3: return JsonResponse({"areas": []})
    try: areas = _shipping_provider().search_areas(query)
    except ProviderError as error: return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({"areas": areas})


def shipping_rates(request):
    if request.method != "POST" or not request.store: raise Http404
    lines, subtotal = get_cart(request)
    postal_code = request.POST.get("postal_code", "").strip()
    area_id = request.POST.get("destination_area_id", "").strip()
    if not lines:
        return JsonResponse({"error": "Keranjang kosong. Tambahkan produk sebelum mengecek ongkir.",
            "code": "empty_cart"}, status=422)
    if not area_id and (not postal_code.isdigit() or len(postal_code) != 5):
        return JsonResponse({"error": "Pilih alamat dari hasil pencarian kurir atau masukkan kode pos 5 digit.",
            "code": "invalid_destination"}, status=422)
    try:
        if settings.SHIPPING_RATE_PROVIDER == "rajaongkir":
            provider = _shipping_provider()
            if not area_id:
                destination = provider.resolve_postal_code(postal_code)
                area_id = destination["id"]
            quotes = provider.rates(area_id, lines)
        else:
            quotes = _shipping_provider().rates(postal_code, lines, area_id)
    except ProviderError as error:
        if "sufficient balance" not in str(error).lower():
            return JsonResponse({"error": str(error)}, status=502)
        methods = ShippingMethod.objects.filter(store=request.store, is_active=True)
        stored, response_quotes = {}, []
        for method in methods:
            token = uuid.uuid4().hex
            price = int(method.quote(subtotal))
            stored[token] = {"provider": "demo", "courier_code": method.code,
                "service_code": method.code, "name": f"{method.name} (tarif demo)", "price": price,
                "eta": method.eta, "postal_code": postal_code, "destination_area_id": area_id,
                "cart_signature": _cart_signature(lines)}
            response_quotes.append({"token": token, "name": f"{method.name} (tarif demo)",
                "price": price, "eta": method.eta})
        if not response_quotes: return JsonResponse({"error": str(error)}, status=402)
        request.session["shipping_quotes"] = stored
        return JsonResponse({"quotes": response_quotes, "fallback": True,
            "provider_name": _shipping_provider_name(),
            "warning": "Saldo test Biteship tidak cukup. Harga yang ditampilkan adalah tarif demo toko, bukan tarif kurir real-time."})
    stored = {}
    response_quotes = []
    for quote in quotes:
        token = uuid.uuid4().hex
        stored[token] = {"provider": quote.provider, "courier_code": quote.courier_code,
            "service_code": quote.service_code, "name": quote.name, "price": quote.price, "eta": quote.eta,
            "postal_code": postal_code, "destination_area_id": area_id,
            "cart_signature": _cart_signature(lines)}
        response_quotes.append({"token": token, "name": quote.name, "price": quote.price, "eta": quote.eta})
    request.session["shipping_quotes"] = stored
    return JsonResponse({"quotes": response_quotes, "provider_name": _shipping_provider_name(),
        "destination_area_id": area_id, "postal_code": postal_code})


def cart_add(request, product_id):
    if request.method != "POST" or not request.store: raise Http404
    product = get_object_or_404(Product, id=product_id, store=request.store, is_active=True)
    cart = request.session.get("cart", {})
    product = Product.objects.prefetch_related("inventory_levels").get(id=product.id)
    variant = None
    variant_id = request.POST.get("variant_id", "").strip()
    if product.variants.filter(is_active=True).exists():
        variant = (ProductVariant.objects.filter(id=variant_id, product=product, is_active=True).first()
            if variant_id.isdigit() else None)
        if not variant:
            messages.error(request, "Pilih varian produk terlebih dahulu.")
            return redirect("product-detail", slug=product.slug)
    key = f"v:{variant.id}" if variant else f"p:{product.id}"
    available = variant.available_stock if variant else product.available_stock
    if available <= 0:
        messages.error(request, f"Stok {product.name} sedang habis.")
        return redirect("product-detail", slug=product.slug)
    try: requested = max(1, int(request.POST.get("quantity", 1)))
    except ValueError: requested = 1
    try: in_cart = max(0, int(cart.get(key, 0)))
    except (TypeError, ValueError): in_cart = 0
    cart[key] = min(in_cart + requested, available)
    request.session["cart"] = cart
    lines, subtotal = get_cart(request)
    update_cart_snapshot(request, lines, subtotal)
    record_event(request, "cart_add", product=product, metadata={"quantity": requested}, always=True)
    messages.success(request, f"{product.name} ditambahkan ke keranjang.")
    return redirect("checkout" if request.POST.get("next") == "checkout" else "cart")

def cart_update(request, item_key):
    if request.method != "POST" or not request.store: raise Http404
    kind, _, raw_id = item_key.partition(":")
    if not raw_id: kind, raw_id = "p", kind
    if kind == "v":
        variant = get_object_or_404(ProductVariant.objects.select_related("product"), id=raw_id,
            product__store=request.store, product__is_active=True, is_active=True)
        available = variant.available_stock
        product = variant.product
    else:
        product = get_object_or_404(Product.objects.prefetch_related("inventory_levels"), id=raw_id, store=request.store)
        available = product.available_stock
    try: quantity = max(0, min(int(request.POST.get("quantity", 1)), available))
    except ValueError: quantity = 1
    cart = request.session.get("cart", {})
    if quantity: cart[item_key] = quantity
    else: cart.pop(item_key, None)
    request.session["cart"] = cart
    lines, subtotal = get_cart(request)
    update_cart_snapshot(request, lines, subtotal)
    record_event(request, "cart_update", product=product, metadata={"quantity": quantity}, always=True)
    return redirect("cart")

def cart_recovery_opt_in(request):
    if request.method != "POST" or not request.store:
        raise Http404
    session = commerce_session(request)
    if request.POST.get("recovery_consent") != "on":
        CommerceSession.objects.filter(id=session.id).update(recovery_email="",
            recovery_consent_at=None, recovery_sent_at=None)
        messages.success(request, "Pengingat keranjang dinonaktifkan.")
        return redirect("cart")
    email = request.POST.get("recovery_email", "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Masukkan email yang valid untuk pengingat keranjang.")
        return redirect("cart")
    CommerceSession.objects.filter(id=session.id).update(recovery_email=email,
        recovery_consent_at=timezone.now(), recovery_sent_at=None)
    messages.success(request, "Pengingat satu kali akan dikirim jika keranjang ditinggalkan.")
    return redirect("cart")


def cart_recover(request, token):
    session = get_object_or_404(CommerceSession, recovery_token=token,
        converted_at__isnull=True, cart_item_count__gt=0)
    cart = {}
    for item in session.cart_snapshot:
        key = str(item.get("key", ""))
        try:
            quantity = max(1, int(item.get("quantity", 1)))
        except (TypeError, ValueError):
            continue
        if key.startswith(("p:", "v:")):
            cart[key] = quantity
    request.session["cart"] = cart
    request.session["store_slug"] = session.store.slug
    request.session["commerce_visitor_id"] = str(session.visitor_id)
    messages.success(request, "Keranjang Anda dipulihkan. Harga dan stok telah diperiksa kembali.")
    return redirect(f"/cart/?store={session.store.slug}")


def cart_view(request):
    if not request.store: raise Http404
    lines, subtotal = get_cart(request)
    update_cart_snapshot(request, lines, subtotal)
    return render(request, "storefront/cart.html", {"lines": lines, "subtotal": subtotal,
        "recovery_session": commerce_session(request)})

@login_required(login_url="/account/login/")
def checkout(request):
    if not request.store: raise Http404
    checkout_key = request.POST.get("idempotency_key") or request.session.get("checkout_key")
    if not checkout_key:
        checkout_key = uuid.uuid4().hex
        request.session["checkout_key"] = checkout_key
    if request.method == "POST":
        existing = Order.objects.filter(store=request.store, idempotency_key=checkout_key).first()
        if existing:
            if existing.payment_checkout_url:
                return redirect(existing.payment_checkout_url)
            return redirect("order-detail", order_id=existing.id)
    lines, subtotal = get_cart(request)
    if not lines: return redirect("cart")
    record_event(request, "checkout_started", metadata={"cart_value": str(subtotal)}, always=True)
    biteship_enabled = _shipping_provider_enabled()
    shipping_methods = [] if biteship_enabled else list(ShippingMethod.objects.filter(store=request.store, is_active=True))
    requested_shipping = request.POST.get("shipping_method", "")
    selected_shipping = next((method for method in shipping_methods if method.code == requested_shipping), None)
    live_quote = request.session.get("shipping_quotes", {}).get(requested_shipping) if biteship_enabled else None
    if not selected_shipping and shipping_methods and (not requested_shipping or request.method != "POST"):
        selected_shipping = shipping_methods[0]
    if not shipping_methods and not biteship_enabled:
        selected_shipping = ShippingMethod(store=request.store, code="pickup", name="Ambil di toko", flat_rate=0)
        shipping_methods = [selected_shipping]
    shipping_cost = Decimal(live_quote["price"]) if live_quote else (selected_shipping.quote(subtotal) if selected_shipping else Decimal(0))
    coupon = get_coupon(request.store, request.POST.get("coupon_code", ""), subtotal) if request.method == "POST" else None
    base_price = calculate_price(store=request.store, lines=lines, shipping_cost=shipping_cost, coupon=coupon)
    loyalty_points, loyalty_discount = redemption_quote(request.store, request.user,
        request.POST.get("loyalty_points", ""), base_price.subtotal - base_price.discount)
    price = calculate_price(store=request.store, lines=lines, shipping_cost=shipping_cost, coupon=coupon,
        additional_discount=loyalty_discount)
    discount, shipping_cost = price.discount, price.shipping
    if request.method == "POST":
        required = ["customer_name", "phone", "shipping_address"]
        if any(not request.POST.get(field, "").strip() for field in required):
            messages.error(request, "Semua data pelanggan wajib diisi.")
        elif not selected_shipping and not live_quote:
            messages.error(request, "Pilih layanan pengiriman.")
        elif live_quote and live_quote["postal_code"] != request.POST.get("postal_code", "").strip():
            messages.error(request, "Kode pos berubah. Silakan cek ulang ongkir.")
        elif (live_quote and request.POST.get("destination_area_id", "").strip() and
                live_quote["destination_area_id"] != request.POST.get("destination_area_id", "").strip()):
            messages.error(request, "Alamat tujuan berubah. Silakan cek ulang ongkir.")
        elif live_quote and live_quote["cart_signature"] != _cart_signature(lines):
            messages.error(request, "Keranjang berubah. Silakan cek ulang ongkir.")
        elif settings.PAYMENT_PROVIDER == "xendit" and not settings.XENDIT_SECRET_KEY:
            messages.error(request, "Xendit belum dikonfigurasi. Tambahkan secret key pada environment.")
        elif settings.PAYMENT_PROVIDER == "doku" and not (
                settings.DOKU_CLIENT_ID and settings.DOKU_SECRET_KEY):
            messages.error(request,
                "DOKU belum dikonfigurasi. Tambahkan client ID dan secret key pada environment.")
        else:
            try: phone = normalize_indonesian_phone(request.POST["phone"])
            except ValueError as error:
                messages.error(request, str(error))
                return render(request, "storefront/checkout.html", {"lines": lines, "subtotal": subtotal, "shipping_methods": shipping_methods})
            with transaction.atomic():
                offer_lines = [line for line in lines if line["sale_offer"]]
                locked_offers = active_sale_offers(store=request.store,
                    product_ids=[line["product"].id for line in offer_lines], lock=True)
                for offer in {line["sale_offer"].id: line["sale_offer"] for line in offer_lines}.values():
                    locked = locked_offers.get(offer.product_id)
                    requested_units = sum(line["quantity"] for line in offer_lines
                        if line["sale_offer"].id == offer.id)
                    if not locked or locked.id != offer.id or locked.sale_price != offer.sale_price:
                        messages.error(request, "Flash sale telah berubah atau berakhir. Harga keranjang diperbarui.")
                        return redirect("checkout")
                    if locked.stock_limit is not None and (
                            offer_sold_quantity(locked) + requested_units > locked.stock_limit):
                        messages.error(request, "Kuota stok flash sale tidak mencukupi.")
                        return redirect("cart")
                    if locked.per_customer_limit is not None and (offer_sold_quantity(
                            locked, phone=phone) + requested_units > locked.per_customer_limit):
                        messages.error(request, "Batas pembelian flash sale per pelanggan telah tercapai.")
                        return redirect("cart")
                    for line in offer_lines:
                        if line["sale_offer"].id == locked.id:
                            line["sale_offer"] = locked
                            line["unit_price"] = locked.sale_price
                            line["total"] = locked.sale_price * line["quantity"]
                if coupon:
                    coupon = Coupon.objects.select_for_update().get(id=coupon.id, store=request.store)
                    if (not coupon.is_active or (coupon.usage_limit is not None and
                            coupon.used_count >= coupon.usage_limit)):
                        messages.error(request, "Voucher sudah tidak tersedia.")
                        return redirect("checkout")
                base_price = calculate_price(store=request.store, lines=lines,
                    shipping_cost=shipping_cost, coupon=coupon)
                locked_loyalty = loyalty_account(request.store, request.user, lock=True)
                loyalty_points, loyalty_discount = redemption_quote(request.store, request.user,
                    request.POST.get("loyalty_points", ""), base_price.subtotal - base_price.discount,
                    account=locked_loyalty)
                price = calculate_price(store=request.store, lines=lines,
                    shipping_cost=shipping_cost, coupon=coupon, additional_discount=loyalty_discount)
                product_ids = [x["product"].id for x in lines if not x["variant"]]
                variant_ids = [x["variant"].id for x in lines if x["variant"]]
                locked = {p.id: p for p in Product.objects.select_for_update().filter(id__in=product_ids)}
                locked_variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(
                    id__in=variant_ids, product__store=request.store, is_active=True,
                ).select_related("product")}
                levels = list(InventoryLevel.objects.select_for_update().filter(
                    product_id__in=product_ids, warehouse__store=request.store, warehouse__is_active=True,
                ).select_related("warehouse").order_by("warehouse__priority", "warehouse_id"))
                by_product = {product_id: [] for product_id in product_ids}
                for level in levels: by_product[level.product_id].append(level)
                insufficient = any(
                    (locked_variants.get(x["variant"].id).available_stock if locked_variants.get(x["variant"].id) else 0) < x["quantity"]
                    if x["variant"] else sum(level.available for level in by_product[x["product"].id]) < x["quantity"]
                    for x in lines
                )
                if insufficient:
                    messages.error(request, "Stok berubah. Silakan periksa keranjang.")
                    return redirect("cart")
                order = Order.objects.create(store=request.store,
                    customer_user=request.user if request.user.is_authenticated else None,
                    subtotal=price.subtotal, tax=price.tax,
                    shipping_cost=price.shipping, discount=price.discount, total=price.total,
                    loyalty_discount=loyalty_discount, loyalty_points_redeemed=loyalty_points,
                    customer_name=request.POST["customer_name"].strip(), email=request.POST.get("email", "").strip(),
                    phone=phone, shipping_address=request.POST["shipping_address"].strip(),
                    province=request.POST.get("province", "").strip(), city=request.POST.get("city", "").strip(),
                    district=request.POST.get("district", "").strip(), village=request.POST.get("village", "").strip(),
                    postal_code=request.POST.get("postal_code", "").strip(), delivery_notes=request.POST.get("delivery_notes", "").strip(),
                    destination_area_id=(live_quote["destination_area_id"] if live_quote else
                        request.POST.get("destination_area_id", "").strip()),
                    payment_provider=settings.PAYMENT_PROVIDER, payment_method=request.POST.get("payment_method", "qris"),
                    shipping_provider=live_quote["provider"] if live_quote else selected_shipping.provider,
                    courier_code=live_quote["courier_code"] if live_quote else selected_shipping.code,
                    courier_service_code=live_quote["service_code"] if live_quote else "",
                    payment_expires_at=timezone.now() + PAYMENT_SESSION_CREATION_WINDOW,
                    shipping_service=live_quote["name"] if live_quote else selected_shipping.name,
                    shipping_eta=live_quote["eta"] if live_quote else selected_shipping.eta,
                    coupon_code=coupon.code if coupon else "", idempotency_key=checkout_key)
                redeem_for_order(order, loyalty_points)
                for line in lines:
                    variant = locked_variants.get(line["variant"].id) if line["variant"] else None
                    product = variant.product if variant else locked[line["product"].id]
                    item = OrderItem.objects.create(order=order, product=product, variant=variant,
                        name=product.name, sku=variant.sku if variant else product.sku,
                        variant_name=variant.name if variant else "", flash_sale_offer=line["sale_offer"],
                        unit_price=line["unit_price"],
                        quantity=line["quantity"], line_total=line["total"])
                    if variant:
                        reserve_variant(item, variant, line["quantity"])
                        continue
                    remaining = line["quantity"]
                    for level in by_product[product.id]:
                        allocated = min(remaining, level.available)
                        if not allocated: continue
                        previous_reserved = level.reserved
                        level.reserved += allocated
                        level.save(update_fields=["reserved", "updated_at"])
                        InventoryAllocation.objects.create(order_item=item, inventory_level=level, quantity=allocated)
                        record_level_reservation(item, level, allocated, previous_reserved)
                        remaining -= allocated
                        if remaining == 0: break
                provider = get_payment_provider(order.payment_provider)
                order.payment_reference = reserved_reference(provider, order)
                order.save(update_fields=["payment_reference"])
                Customer.objects.update_or_create(store=request.store, phone=phone, defaults={"name": order.customer_name,
                    "email": order.email, "accepts_marketing": request.POST.get("accepts_marketing") == "on"})
                OrderStatusEvent.objects.create(order=order, status="pending", note="Pesanan dibuat")
                if coupon:
                    coupon.used_count += 1
                    coupon.save(update_fields=["used_count"])
            # Stock, coupon and loyalty locks are released before the gateway call. If the process
            # dies here, the creation window above lets the expiry worker release the reservation.
            remember_customer_order(request, order)
            try:
                payment = provider.create_payment(order)
            except ProviderError as error:
                _abandon_unpaid_order(order, coupon)
                request.session["checkout_key"] = uuid.uuid4().hex
                if (order.payment_provider == "doku" and
                        "PLEASE USE ANOTHER PAYMENT METHOD" in str(error)):
                    messages.error(request,
                        "Metode pembayaran DOKU belum tersedia. Silakan coba kembali atau "
                        "hubungi toko untuk pilihan pembayaran lain.")
                else:
                    messages.error(request,
                        "Pembayaran belum dapat dibuat. Silakan coba beberapa saat lagi.")
                return redirect("checkout")
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=order.id)
                order.payment_reference = payment.reference
                order.payment_checkout_url = payment.checkout_url
                if payment.expires_at:
                    order.payment_expires_at = parse_datetime(payment.expires_at)
                order.save(update_fields=["payment_reference", "payment_checkout_url", "payment_expires_at"])
                PaymentTransaction.objects.create(order=order, provider=order.payment_provider,
                    external_id=payment.reference, status="pending", amount=order.total, currency="IDR")
                OutboxEvent.objects.create(store=request.store, topic="order.created", aggregate_id=str(order.id),
                    payload={"order_id": str(order.id), "phone": order.phone, "email": order.email,
                        "total": str(order.total), "payment_method": order.payment_method})
            request.session["cart"] = {}
            request.session["checkout_key"] = uuid.uuid4().hex
            record_event(request, "converted", metadata={"order_id": str(order.id),
                "total": str(order.total)}, always=True)
            update_cart_snapshot(request, [], 0)
            return redirect(payment.checkout_url)
    loyalty = loyalty_account(request.store, request.user) if request.store.loyalty_enabled else None
    account_name = request.user.get_full_name() if request.user.is_authenticated else ""
    account_email = request.user.email if request.user.is_authenticated else ""
    return render(request, "storefront/checkout.html", {"lines": lines, "subtotal": subtotal,
        "shipping_methods": shipping_methods, "shipping_cost": shipping_cost, "discount": discount,
        "idempotency_key": checkout_key,
        "biteship_enabled": biteship_enabled,
        "biteship_configured": biteship_enabled,
        "shipping_provider_name": _shipping_provider_name(), "account_name": account_name,
        "account_email": account_email, "loyalty_account": loyalty,
        "loyalty_discount": loyalty_discount, "loyalty_points": loyalty_points})


def customer_register(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = ""
    if request.user.is_authenticated:
        return redirect(next_url or "account-orders")
    form = CustomerRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Akun berhasil dibuat. Selamat berbelanja!")
        return redirect(next_url or "account-orders")
    return render(request, "registration/register.html", {"form": form, "next": next_url})


@login_required(login_url="/account/login/")
def account_orders(request):
    if not request.store:
        raise Http404
    orders = Order.objects.filter(store=request.store, customer_user=request.user).prefetch_related(
        "items").order_by("-created_at")
    loyalty = LoyaltyAccount.objects.filter(store=request.store, user=request.user).prefetch_related(
        "transactions").first()
    return render(request, "storefront/account_orders.html", {"orders": orders, "loyalty_account": loyalty})

def order_detail(request, order_id):
    if not request.store: raise Http404
    order = get_customer_order_or_404(
        request, order_id, queryset=Order.objects.prefetch_related(
            "items", "status_events", "tracking_events")
    )
    return render(request, "storefront/order.html", {"order": order})

def request_return(request, order_id):
    if request.method != "POST" or not request.store: raise Http404
    order = get_customer_order_or_404(request, order_id)
    reason = request.POST.get("reason", "").strip()
    if reason and order.status in {"paid", "confirmed", "allocated", "picking", "packed", "shipped", "delivered", "completed"}:
        ReturnRequest.objects.create(order=order, reason=reason)
        messages.success(request, "Permintaan pengembalian telah dikirim.")
    else: messages.error(request, "Permintaan pengembalian belum dapat diproses.")
    return redirect("order-detail", order_id=order.id)

def _staff_store(request):
    stores = Store.objects.filter(owner=request.user) | Store.objects.filter(staff_members__user=request.user)
    stores = stores.distinct().order_by("name")
    requested = request.GET.get("store") or request.POST.get("store")
    if requested and stores.filter(id=requested).exists():
        request.session["merchant_store_id"] = int(requested)
    store = stores.filter(id=request.session.get("merchant_store_id")).first() or stores.first()
    if store:
        request.session["merchant_store_id"] = store.id
        return store
    raise Http404


def _merchant_context(request, store, **context):
    stores = (Store.objects.filter(owner=request.user) |
        Store.objects.filter(staff_members__user=request.user)).distinct().order_by("name")
    role = "admin" if store.owner_id == request.user.id else store.staff_members.get(user=request.user).role
    return {"merchant_store": store, "merchant_stores": stores, "merchant_role": role, **context}


def _require_editor(request, store):
    if store.owner_id == request.user.id:
        return
    membership = store.staff_members.filter(user=request.user).first()
    if not membership or membership.role not in {"admin", "manager", "ops"}:
        raise Http404


def _require_roles(request, store, roles):
    if store.owner_id == request.user.id:
        return
    if not store.staff_members.filter(user=request.user, role__in=roles).exists():
        raise Http404


MERCHANT_PAGE_SIZE = 50


@login_required(login_url="/merchant/login/")
def merchant_dashboard(request):
    store = _staff_store(request)
    revenue_statuses = {"paid", "confirmed", "allocated", "picking", "packed", "shipped",
        "delivered", "completed", "refund_requested", "partially_refunded", "refunded"}
    realized = Order.objects.filter(store=store, status__in=revenue_statuses)
    totals = realized.aggregate(revenue=Sum("total"), orders=Count("id"))
    order_count = totals["orders"] or 0
    revenue = totals["revenue"] or Decimal(0)
    daily = list(realized.filter(created_at__gte=timezone.now() - timedelta(days=30)).annotate(
        day=TruncDate("created_at")).values("day").annotate(
        revenue=Sum("total"), orders=Count("id")).order_by("day"))
    peak = max((row["revenue"] for row in daily), default=Decimal(0))
    for row in daily:
        row["percent"] = round((row["revenue"] / peak * 100), 1) if peak else 0
    top_products = (OrderItem.objects.filter(order__in=realized).values("name").annotate(
        units=Sum("quantity"), revenue=Sum("line_total")).order_by("-revenue")[:5])
    low_stock_products = annotate_available_stock(Product.objects.filter(store=store, is_active=True)
        ).filter(stock_available__lte=5)
    low_stock_count = low_stock_products.count()
    low_stock = list(low_stock_products.order_by("stock_available", "name")[:8])
    recent = Order.objects.filter(store=store).order_by("-created_at")[:8]
    fulfillment = {status: store.orders.filter(status=status).count() for status in
        ("paid", "confirmed", "allocated", "picking", "packed", "shipped")}
    now = timezone.now()
    today = timezone.localdate()
    today_orders = realized.filter(created_at__date=today)
    today_totals = today_orders.aggregate(gross=Sum("total"), orders=Count("id"))
    today_gross = today_totals["gross"] or Decimal(0)
    today_refunds = (Refund.objects.filter(order__store=store, status="succeeded",
        updated_at__date=today).aggregate(total=Sum("amount"))["total"] or Decimal(0))
    today_paid = today_orders.count()
    today_aov = today_gross / today_paid if today_paid else Decimal(0)
    customer_first_orders = (realized.values("phone").annotate(first_order=Min("created_at")))
    new_customers = sum(1 for row in customer_first_orders
        if timezone.localtime(row["first_order"]).date() == today)
    today_customer_phones = set(today_orders.values_list("phone", flat=True))
    repeat_customers = max(0, len(today_customer_phones) - new_customers)
    failed_integrations = OutboxEvent.objects.filter(store=store, status="failed").count()
    pending_events = OutboxEvent.objects.filter(store=store, status__in={"pending", "processing"})
    oldest_pending = pending_events.order_by("created_at").values_list("created_at", flat=True).first()
    queue_backlogged = pending_events.count() >= 100 or bool(
        oldest_pending and oldest_pending < now - timedelta(minutes=5))
    notification_failures = OutboxEvent.objects.filter(store=store, status="failed",
        topic__in={"order.created", "payment.received", "order.status_changed", "order.refunded"}).count()
    payment_errors = PaymentTransaction.objects.filter(order__store=store, status="failed").count()
    reconciliation_mismatches = PaymentTransaction.objects.filter(order__store=store,
        reconciled=False).exclude(status="pending").count()
    shipping_problems = store.orders.filter(status="packed", tracking_number="",
        created_at__lt=now - timedelta(hours=24)).count()
    operational_health = [
        {"name": "Application", "state": "healthy", "label": "Healthy"},
        {"name": "Database", "state": "healthy", "label": "Healthy"},
        {"name": "Payment", "state": "issue" if payment_errors or reconciliation_mismatches else "healthy",
            "label": "Issue" if payment_errors or reconciliation_mismatches else "Healthy"},
        {"name": "Shipping", "state": "issue" if shipping_problems else "healthy",
            "label": "Issue" if shipping_problems else "Healthy"},
        {"name": "Email", "state": "issue" if notification_failures else "healthy",
            "label": "Issue" if notification_failures else "Healthy"},
        {"name": "Queue", "state": "issue" if queue_backlogged else "healthy",
            "label": "Backlogged" if queue_backlogged else "Healthy"},
    ]
    engagement = engagement_summary(store)
    loyalty = LoyaltyAccount.objects.filter(store=store).aggregate(
        members=Count("id"), outstanding=Sum("balance"), earned=Sum("lifetime_earned"))
    return render(request, "merchant/dashboard.html", _merchant_context(request, store,
        revenue=revenue, order_count=order_count, average_order=(revenue / order_count if order_count else 0),
        customer_count=store.customers.count(), pending_count=store.orders.filter(status="pending").count(),
        daily_sales=daily, top_products=top_products, low_stock=low_stock,
        low_stock_count=low_stock_count, recent_orders=recent,
        fulfillment=fulfillment, failed_integrations=failed_integrations,
        reconciliation_mismatches=reconciliation_mismatches, today_gross=today_gross,
        today_net=today_gross - today_refunds, today_orders=today_totals["orders"] or 0,
        today_paid=today_paid, today_aov=today_aov, today_new_customers=new_customers,
        today_repeat_customers=repeat_customers, today_refunds=today_refunds,
        payment_errors=payment_errors, shipping_problems=shipping_problems,
        notification_failures=notification_failures, queue_pending=pending_events.count(),
        queue_backlogged=queue_backlogged, operational_health=operational_health,
        engagement=engagement, loyalty=loyalty))

@login_required(login_url="/merchant/login/")
def merchant_orders(request):
    store = _staff_store(request)
    orders = Order.objects.filter(store=store).prefetch_related(
        "items", "tracking_events").order_by("-created_at")
    status = request.GET.get("status")
    if status: orders = orders.filter(status=status)
    orders = list(orders)
    for order in orders:
        order.available_statuses = allowed_order_transitions(order)
        order.can_book_shipment = can_book_shipment(order)
        order.booking_pending = is_booking_pending(order)
    return render(request, "merchant/orders.html", _merchant_context(request, store,
        orders=orders, statuses=Order.STATUS, fulfillment_methods=Order.FULFILLMENT_METHODS))


@login_required(login_url="/merchant/login/")
def merchant_stock(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    back = request.get_full_path()
    if request.method == "POST":
        target_type = request.POST.get("target_type")
        try:
            new_stock = int(request.POST.get("stock", ""))
            safety_stock = int(request.POST.get("safety_stock", "0"))
            if new_stock < 0 or safety_stock < 0:
                raise ValueError
        except ValueError:
            messages.error(request, "Stok harus berupa angka nol atau lebih.")
            return redirect(back)
        reason = request.POST.get("reason", "Penyesuaian stok dashboard").strip()[:200]
        with transaction.atomic():
            if target_type == "level":
                level = get_object_or_404(InventoryLevel.objects.select_for_update(),
                    id=request.POST.get("target_id"), warehouse__store=store)
                if new_stock < level.reserved:
                    messages.error(request,
                        f"Stok tidak boleh kurang dari {level.reserved} unit yang sedang direservasi.")
                    return redirect(back)
                previous = level.on_hand
                level.on_hand, level.safety_stock = new_stock, safety_stock
                level.save(update_fields=["on_hand", "safety_stock", "updated_at"])
                if previous != new_stock:
                    record_movement(store=store, inventory_level=level, kind="adjust",
                        quantity=abs(new_stock - previous), reason=reason, actor=request.user,
                        previous_on_hand=previous, new_on_hand=new_stock,
                        previous_reserved=level.reserved, new_reserved=level.reserved)
                label = f"{level.product.name} · {level.warehouse.name}"
            elif target_type == "variant":
                variant = get_object_or_404(ProductVariant.objects.select_for_update(),
                    id=request.POST.get("target_id"), product__store=store)
                if new_stock < variant.reserved:
                    messages.error(request,
                        f"Stok tidak boleh kurang dari {variant.reserved} unit yang sedang direservasi.")
                    return redirect(back)
                previous = variant.stock
                variant.stock = new_stock
                variant.save(update_fields=["stock"])
                if previous != new_stock:
                    record_movement(store=store, variant=variant, kind="adjust",
                        quantity=abs(new_stock - previous), reason=reason, actor=request.user,
                        previous_on_hand=previous, new_on_hand=new_stock,
                        previous_reserved=variant.reserved, new_reserved=variant.reserved)
                label = str(variant)
            else:
                raise Http404
            AuditLog.objects.create(store=store, actor=request.user, action="inventory.adjusted",
                object_type=target_type, object_id=request.POST.get("target_id", ""),
                details={"label": label, "new_stock": new_stock, "reason": reason})
        messages.success(request, f"Stok {label} berhasil diperbarui.")
        return redirect(back)
    levels = InventoryLevel.objects.filter(warehouse__store=store).select_related(
        "product", "warehouse").order_by("product__name", "warehouse__priority", "id")
    variants = ProductVariant.objects.filter(product__store=store, is_active=True).select_related(
        "product").order_by("product__name", "name", "id")
    query = request.GET.get("q", "").strip()
    if query:
        levels = levels.filter(models.Q(product__name__icontains=query) |
            models.Q(product__sku__icontains=query))
        variants = variants.filter(models.Q(product__name__icontains=query) |
            models.Q(name__icontains=query) | models.Q(sku__icontains=query))
    levels, level_range, level_query = paginate(request, levels, MERCHANT_PAGE_SIZE)
    variants, variant_range, variant_query = paginate(request, variants, MERCHANT_PAGE_SIZE,
        param="vpage")
    return render(request, "merchant/stock.html", _merchant_context(request, store,
        levels=levels, level_range=level_range, level_query=level_query, variants=variants,
        variant_range=variant_range, variant_query=variant_query, query=query))

@login_required(login_url="/merchant/login/")
def merchant_order_update(request, order_id):
    if request.method != "POST": raise Http404
    store = _staff_store(request)
    new_status = request.POST.get("status", "")
    if new_status in {"refund_requested", "partially_refunded", "refunded"}:
        messages.error(request, "Status refund hanya dapat diubah melalui proses refund terverifikasi.")
        return redirect("merchant-orders")
    fulfilment_roles = {"admin", "manager", "ops", "warehouse"}
    service_roles = {"admin", "manager", "ops", "customer_service"}
    roles = fulfilment_roles if new_status in {"allocated", "picking", "packed", "shipped"} else service_roles
    _require_roles(request, store, roles)
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), id=order_id, store=store)
        old_status = order.status
        posted_tracking = request.POST.get("tracking_number")
        tracking_number = (posted_tracking.strip() if posted_tracking is not None
            else order.tracking_number)
        if new_status == "shipped" and not tracking_number:
            messages.error(request, "Nomor resi wajib diisi sebelum pesanan dikirim.")
            return redirect("merchant-orders")
        try:
            transition_order(order, new_status, note=request.POST.get("note", "").strip(),
                actor=request.user)
        except InvalidOrderTransition as error:
            messages.error(request, str(error))
            return redirect("merchant-orders")
        if posted_tracking is not None and tracking_number != order.tracking_number:
            order.tracking_number = tracking_number
            order.tracking_status = "ready_to_track" if tracking_number else "not_ready"
            order.save(update_fields=["tracking_number", "tracking_status"])
        AuditLog.objects.create(store=store, actor=request.user, action="order.status_updated",
            object_type="Order", object_id=str(order.id), details={"from": old_status, "to": new_status})
    messages.success(request, "Pesanan diperbarui.")
    return redirect("merchant-orders")


@login_required(login_url="/merchant/login/")
def merchant_order_tracking(request, order_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    order = get_object_or_404(Order, id=order_id, store=store)
    if order.status in {"pending", "payment_failed", "cancelled", "refunded"}:
        messages.error(request, "Pengiriman belum dapat diatur untuk status pesanan ini.")
        return redirect("merchant-orders")
    tracking_number = request.POST.get("tracking_number", "").strip()[:100]
    courier_code = request.POST.get("courier_code", "").strip().lower()[:40]
    method = request.POST.get("fulfillment_method", "merchant_pickup")
    if method not in dict(Order.FULFILLMENT_METHODS):
        messages.error(request, "Metode fulfillment tidak valid.")
        return redirect("merchant-orders")
    if not tracking_number or not re.fullmatch(r"[A-Za-z0-9._/-]+", tracking_number):
        messages.error(request, "Masukkan nomor resi yang valid.")
        return redirect("merchant-orders")
    if not courier_code or not re.fullmatch(r"[a-z0-9_-]+", courier_code):
        messages.error(request, "Masukkan kode kurir, misalnya jne, jnt, sicepat, atau anteraja.")
        return redirect("merchant-orders")
    old_tracking = order.tracking_number
    order.tracking_number = tracking_number
    order.courier_code = courier_code
    order.fulfillment_method = method
    order.tracking_status = "ready_to_track"
    order.tracking_status_detail = "Nomor resi tersimpan dan siap disinkronkan."
    order.tracking_error = ""
    order.tracking_last_checked_at = None
    order.save(update_fields=["tracking_number", "courier_code", "fulfillment_method",
        "tracking_status", "tracking_status_detail", "tracking_error",
        "tracking_last_checked_at"])
    if old_tracking and old_tracking != tracking_number:
        order.tracking_events.all().delete()
    AuditLog.objects.create(store=store, actor=request.user, action="order.tracking_updated",
        object_type="Order", object_id=str(order.id), details={"tracking_number": tracking_number,
            "courier_code": courier_code, "fulfillment_method": method})
    if request.POST.get("sync_now"):
        try:
            sync_order_tracking(order)
        except ProviderError as error:
            order.tracking_error = str(error)[:300]
            order.tracking_last_checked_at = timezone.now()
            order.save(update_fields=["tracking_error", "tracking_last_checked_at"])
            messages.warning(request,
                "Resi tersimpan, tetapi status kurir belum dapat disinkronkan: " + str(error))
        else:
            messages.success(request, "Resi tersimpan dan status kurir berhasil disinkronkan.")
    else:
        messages.success(request, "Nomor resi dan metode fulfillment berhasil disimpan.")
    return redirect("merchant-orders")


@login_required(login_url="/merchant/login/")
def merchant_order_book_shipment(request, order_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    order = get_object_or_404(Order, id=order_id, store=store)
    try:
        result = book_shipment(order, actor=request.user)
    except (ShipmentBookingError, ProviderError) as error:
        messages.error(request, f"Kurir belum dapat dipesan: {error}")
    else:
        waybill = result["waybill_id"] or "menyusul setelah pickup"
        messages.success(request, f"Kurir dipesan. Nomor resi: {waybill}.")
    return redirect("merchant-orders")


@login_required(login_url="/merchant/login/")
def merchant_order_refund(request, order_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "finance"})
    order = get_object_or_404(Order, id=order_id, store=store)
    manual = request.POST.get("method") == "manual_transfer"
    transfer_reference = request.POST.get("transfer_reference", "").strip()
    if manual and not transfer_reference:
        messages.error(request, "Nomor referensi transfer wajib diisi untuk refund manual.")
        return redirect("merchant-orders")
    try:
        refund = create_refund_request(order=order, amount=request.POST.get("amount", ""),
            reason=request.POST.get("reason", "OTHERS"), actor=request.user,
            note=request.POST.get("note", "").strip(),
            method="manual_transfer" if manual else "gateway")
        if manual:
            record_manual_refund(refund, transfer_reference=transfer_reference, actor=request.user)
        else:
            execute_refund(refund)
    except (RefundError, ProviderError, ValueError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Refund berhasil diproses dan dicatat.")
    return redirect("merchant-orders")

@login_required(login_url="/merchant/login/")
def merchant_orders_csv(request):
    store = _staff_store(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders.csv"'
    writer = csv.writer(response)
    writer.writerow(["order_id", "created_at", "customer", "phone", "status", "subtotal", "discount", "shipping", "tax", "total", "tracking"])
    for order in Order.objects.filter(store=store).order_by("-created_at"):
        writer.writerow([order.id, order.created_at.isoformat(), order.customer_name, order.phone, order.status,
            order.subtotal, order.discount, order.shipping_cost, order.tax, order.total, order.tracking_number])
    return response


BACKOFFICE = {
    "flash-sales": (FlashSaleProduct, FlashSaleProductForm, "Produk flash sale", ("campaign", "product", "sale_price", "stock_limit", "per_customer_limit")),
    "campaigns": (MarketingCampaign, MarketingCampaignForm, "Flash sale & popup", ("name", "headline", "starts_at", "ends_at", "is_active")),
    "slides": (StorefrontSlide, StorefrontSlideForm, "Banner carousel", ("headline", "button_label", "position", "is_active")),
    "products": (Product, ProductForm, "Produk", ("name", "sku", "price", "stock_available", "is_active")),
    "images": (ProductImage, ProductImageForm, "Foto produk", ("product", "alt_text", "position")),
    "variants": (ProductVariant, VariantForm, "Varian", ("product", "name", "sku", "price", "stock", "is_active")),
    "categories": (Category, CategoryForm, "Kategori", ("name", "slug")),
    "warehouses": (Warehouse, WarehouseForm, "Gudang", ("name", "code", "priority", "is_active")),
    "inventory": (InventoryLevel, InventoryForm, "Persediaan", ("warehouse", "product", "on_hand", "reserved", "safety_stock", "available")),
    "shipping": (ShippingMethod, ShippingMethodForm, "Pengiriman", ("name", "provider", "flat_rate", "free_above", "is_active")),
    "coupons": (Coupon, CouponForm, "Voucher", ("code", "kind", "value", "minimum_spend", "used_count", "is_active")),
}


def _entity_queryset(entity, store):
    if entity in {Product, Category, Warehouse, ShippingMethod, Coupon, StorefrontSlide, MarketingCampaign}:
        return entity.objects.filter(store=store)
    if entity is FlashSaleProduct:
        return entity.objects.filter(product__store=store, campaign__store=store).select_related("product", "campaign")
    if entity is ProductVariant:
        return entity.objects.filter(product__store=store).select_related("product")
    if entity is ProductImage:
        return entity.objects.filter(product__store=store).select_related("product")
    if entity is InventoryLevel:
        return entity.objects.filter(warehouse__store=store).select_related("warehouse", "product")
    raise Http404


@login_required(login_url="/merchant/login/")
def merchant_entity_list(request, section):
    store = _staff_store(request)
    if section not in BACKOFFICE:
        raise Http404
    entity, _, title, columns = BACKOFFICE[section]
    rows = _entity_queryset(entity, store).order_by("-id")
    if entity is Product:
        rows = annotate_available_stock(rows)
    query = request.GET.get("q", "").strip()
    if query:
        if entity is Product or entity is ProductVariant: rows = rows.filter(models.Q(name__icontains=query) | models.Q(sku__icontains=query))
        elif entity is Coupon: rows = rows.filter(code__icontains=query)
        elif hasattr(entity, "name"): rows = rows.filter(name__icontains=query)
    page, page_range, page_query = paginate(request, rows, MERCHANT_PAGE_SIZE)
    table_rows = [{"object": row, "values": [getattr(row, column) for column in columns]}
        for row in page.object_list]
    return render(request, "merchant/entity_list.html", _merchant_context(request, store,
        section=section, title=title, columns=columns, table_rows=table_rows, page_obj=page,
        page_range=page_range, page_query=page_query))


@login_required(login_url="/merchant/login/")
def merchant_entity_edit(request, section, object_id=None):
    store = _staff_store(request)
    if section not in BACKOFFICE:
        raise Http404
    if section in {"inventory", "warehouses"}:
        _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    else:
        _require_editor(request, store)
    entity, form_class, title, _ = BACKOFFICE[section]
    instance = get_object_or_404(_entity_queryset(entity, store), id=object_id) if object_id else None
    form = form_class(request.POST or None, request.FILES or None, instance=instance, store=store)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        if hasattr(saved, "store_id"):
            saved.store = store
        saved.save()
        form.save_m2m()
        if entity is Product and not instance:
            warehouse = (form.cleaned_data.get("warehouse") or
                Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id").first())
            if warehouse:
                InventoryLevel.objects.get_or_create(warehouse=warehouse, product=saved,
                    defaults={"on_hand": form.cleaned_data.get("initial_stock") or 0})
        AuditLog.objects.create(store=store, actor=request.user,
            action=f"{section}.{'updated' if instance else 'created'}", object_type=entity.__name__,
            object_id=str(saved.id), details={"label": str(saved)})
        messages.success(request, f"{title} berhasil disimpan.")
        return redirect("merchant-entity-list", section=section)
    return render(request, "merchant/entity_form.html", _merchant_context(request, store,
        section=section, title=title, form=form, editing=bool(instance)))


@login_required(login_url="/merchant/login/")
def merchant_settings(request):
    store = _staff_store(request)
    _require_editor(request, store)
    form = StoreSettingsForm(request.POST or None, request.FILES or None,
        instance=store, store=store)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(store=store, actor=request.user, action="store.settings_updated",
            object_type="Store", object_id=str(store.id), details={"fields": list(form.changed_data)})
        messages.success(request, "Pengaturan toko berhasil disimpan.")
        return redirect("merchant-settings")
    return render(request, "merchant/settings.html", _merchant_context(request, store,
        title="Setup cepat", form=form, editing=True, section="settings",
        storefront_templates=STOREFRONT_TEMPLATES))


@login_required(login_url="/merchant/login/")
def merchant_apply_template(request):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_editor(request, store)
    key = request.POST.get("template", "")
    if key not in STOREFRONT_TEMPLATES:
        messages.error(request, "Template tidak dikenal.")
        return redirect("merchant-settings")
    preset = apply_storefront_template(store, key)
    AuditLog.objects.create(store=store, actor=request.user, action="store.template_applied",
        object_type="Store", object_id=str(store.id), details={"template": key})
    messages.success(request, f"Template {preset['label']} diterapkan. Logo, foto sampul, dan teks "
        "Anda tidak berubah.")
    return redirect("merchant-settings")


@login_required(login_url="/merchant/login/")
def merchant_customers(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "customer_service", "viewer"})
    customers = Customer.objects.filter(store=store).order_by("-created_at")
    query = request.GET.get("q", "").strip()
    if query:
        customers = customers.filter(models.Q(name__icontains=query) | models.Q(phone__icontains=query) |
            models.Q(email__icontains=query))
    return render(request, "merchant/customers.html", _merchant_context(request, store, customers=customers))


@login_required(login_url="/merchant/login/")
def merchant_customer_detail(request, customer_id):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "customer_service", "viewer"})
    customer = get_object_or_404(Customer, id=customer_id, store=store)
    orders = Order.objects.filter(store=store, phone=customer.phone).order_by("-created_at")
    stats = orders.exclude(status__in={"pending", "payment_failed", "cancelled"}).aggregate(
        lifetime_spend=Sum("total"), order_count=Count("id"))
    count = stats["order_count"] or 0
    return render(request, "merchant/customer_detail.html", _merchant_context(request, store,
        customer=customer, orders=orders, lifetime_spend=stats["lifetime_spend"] or 0,
        order_count=count, average_order=((stats["lifetime_spend"] or 0) / count if count else 0)))


@login_required(login_url="/merchant/login/")
def merchant_engagement(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "customer_service", "viewer"})
    summary = engagement_summary(store)
    summary["abandoned_sessions"] = summary["abandoned_sessions"][:50]
    summary["engaged_sessions"] = summary["engaged_sessions"][:50]
    return render(request, "merchant/engagement.html",
        _merchant_context(request, store, **summary))


@login_required(login_url="/merchant/login/")
def merchant_integrations(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "finance", "viewer"})
    events = OutboxEvent.objects.filter(store=store).order_by("-created_at")[:100]
    mismatches = PaymentTransaction.objects.filter(order__store=store, reconciled=False).exclude(
        status="pending").select_related("order").order_by("-updated_at")
    return render(request, "merchant/integrations.html", _merchant_context(request, store,
        integration_events=events, reconciliation_mismatches=mismatches))


@login_required(login_url="/merchant/login/")
def merchant_integration_retry(request, event_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "finance"})
    event = get_object_or_404(OutboxEvent, id=event_id, store=store)
    event.requeue()
    AuditLog.objects.create(store=store, actor=request.user, action="integration.retry_requested",
        object_type="OutboxEvent", object_id=str(event.id), details={"topic": event.topic})
    messages.success(request, "Integrasi dijadwalkan untuk dicoba ulang.")
    return redirect("merchant-integrations")
