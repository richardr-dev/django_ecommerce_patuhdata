import csv
import hashlib
import logging
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote as url_quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.validators import validate_email
from django.db import models, transaction
from django.db.models import Count, F, Min, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounting.ledger import (
    COGS,
    GATEWAY,
    MARKETPLACE,
    UNIDENTIFIED,
    VAT,
    account_balances,
)
from apps.accounting.models import JournalLine
from apps.billing.plans import subscription_status
from apps.catalog.models import (
    Category,
    FlashSaleProduct,
    InventoryLevel,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Warehouse,
    annotate_available_stock,
)
from apps.catalog.reviews import REVIEWABLE_STATUSES, order_review_items, save_order_review
from apps.catalog.views import paginate
from apps.integrations.forms import PaymentSettingsForm
from apps.integrations.http import ProviderError
from apps.integrations.models import OutboxEvent, PlatformPayout, StorePaymentSettings
from apps.integrations.payments import (
    DokuPaymentProvider,
    doku_environment,
    order_payment_provider,
    payment_options,
    platform_doku_available,
    reserved_reference,
)
from apps.integrations.platform_balance import balance as platform_balance
from apps.integrations.platform_balance import platform_orders
from apps.integrations.reconciliation import confirm_payment
from apps.integrations.shipping import BiteshipProvider, RajaOngkirProvider, shipping_origin
from apps.shops.availability import preview_url, store_state
from apps.shops.models import (
    AuditLog,
    Customer,
    MarketingCampaign,
    NewsletterSubscriber,
    Store,
    StorefrontSlide,
    StorePage,
)
from apps.shops.shortlinks import attributed_campaign_id
from apps.shops.storefront_templates import STOREFRONT_TEMPLATES, apply_storefront_template
from apps.shops.tenancy import store_url
from apps.shops.trust import store_trust

from .access import (
    find_guest_order,
    get_customer_order_or_404,
    order_code,
    remember_customer_order,
)
from .addresses import checkout_prefill, save_checkout_address, saved_addresses
from .buyer import BUYER_TABS, buyer_orders, estimated_arrival, tracker_steps
from .cart import get_cart
from .couriers import courier_name, courier_tracking, present_quotes
from .engagement import commerce_session, engagement_summary, record_event, update_cart_snapshot
from .forms import (
    CategoryForm,
    CouponForm,
    CustomerAddressForm,
    CustomerRegistrationForm,
    FlashSaleProductForm,
    InventoryForm,
    MarketingCampaignForm,
    MerchantReviewForm,
    OrderReviewForm,
    ProductForm,
    ProductImageForm,
    ProductVariantFormSet,
    ShippingMethodForm,
    StorefrontSlideForm,
    StorePageForm,
    StoreSettingsForm,
    WarehouseForm,
)
from .gift_cards import find as find_gift_card
from .gift_cards import redeem as redeem_gift_card
from .gift_cards import usable_amount as gift_card_usable
from .inventory import record_movement, reserve_from_levels, reserve_variant
from .loyalty import loyalty_account, redeem_for_order, redemption_quote
from .models import (
    CommerceSession,
    Coupon,
    CustomerAddress,
    LoyaltyAccount,
    Order,
    OrderItem,
    OrderStatusEvent,
    PaymentTransaction,
    Refund,
    ReturnRequest,
    ShippingMethod,
)
from .onboarding import setup_steps
from .pricing import calculate_price
from .refunds import RefundError, create_refund_request, execute_refund, record_manual_refund
from .sales import active_sale_offers, offer_sold_quantity
from .scheduling import (
    ScheduleError,
    available_dates,
    needs_schedule,
    orderable_quantity,
    preorder_remaining,
    slot_options,
    validate_schedule,
)
from .services import (
    FULFILMENT_PATH,
    ORDER_GROUPS,
    InvalidOrderTransition,
    advance_order,
    allowed_order_transitions,
    coupon_uses_by_customer,
    get_coupon,
    normalize_indonesian_phone,
    transition_order,
)
from .shipments import (
    ShipmentBookingError,
    book_shipment,
    can_book_shipment,
    courier_label_url,
    has_courier_label,
    is_booking_pending,
    merged_courier_labels,
)
from .tracking import sync_order_tracking

# How long a new order may hold stock while its gateway session is being created.
PAYMENT_SESSION_CREATION_WINDOW = timedelta(minutes=15)

logger = logging.getLogger(__name__)


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


# Shoppers see the courier names, never which rate provider the platform uses behind them.
SHIPPING_UNAVAILABLE = "Layanan kurir sedang tidak dapat dihubungi. Coba lagi sebentar lagi."


def _shopper_shipping_error(error):
    """Our own Indonesian messages pass through; a provider's raw reply does not."""
    text = str(error)
    if text.startswith(("Provider returned", "Provider is")) or "API key" in text or "origin" in text:
        return SHIPPING_UNAVAILABLE
    return text


def _shipping_provider():
    if settings.SHIPPING_RATE_PROVIDER == "rajaongkir": return RajaOngkirProvider()
    return BiteshipProvider()


def _shipping_provider_enabled(store):
    """Live courier rates at checkout: the provider is configured and, for Biteship, the store
    has said where it ships from. Otherwise checkout offers the store's own shipping methods."""
    if settings.SHIPPING_RATE_PROVIDER == "rajaongkir":
        return bool(settings.RAJAONGKIR_API_KEY and
            (settings.RAJAONGKIR_ORIGIN_ID or settings.RAJAONGKIR_ORIGIN_POSTAL_CODE))
    key = settings.BITESHIP_API_KEY
    return bool(key and (not key.startswith("biteship_live.") or settings.BITESHIP_ALLOW_LIVE)
        and shipping_origin(store))


def _shipping_provider_name():
    return "RajaOngkir" if settings.SHIPPING_RATE_PROVIDER == "rajaongkir" else "Biteship"


def shipping_areas(request):
    if not request.store: raise Http404
    query = request.GET.get("q", "").strip()
    if len(query) < 3: return JsonResponse({"areas": []})
    try: areas = _shipping_provider().search_areas(query)
    except ProviderError as error:
        logger.warning("shipping area search failed: %s", error)
        return JsonResponse({"error": SHIPPING_UNAVAILABLE}, status=502)
    return JsonResponse({"areas": areas})


@login_required(login_url="/merchant/login/")
def merchant_shipping_areas(request):
    """Area search for the warehouse form: the courier provider's own list of kecamatan and
    postal codes, so a warehouse's place matches what rates and pickups use."""
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    query = request.GET.get("q", "").strip()
    if len(query) < 3:
        return JsonResponse({"areas": []})
    try:
        areas = _shipping_provider().search_areas(query)
    except ProviderError as error:
        logger.warning("merchant area search failed: %s", error)
        return JsonResponse({"error": SHIPPING_UNAVAILABLE}, status=502)
    return JsonResponse({"areas": areas[:10]})


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
            origin = shipping_origin(request.store)
            if not origin:
                return JsonResponse({"error": "Toko belum mengisi kode pos gudang untuk ongkir "
                    "otomatis.", "code": "no_origin"}, status=422)
            quotes = _shipping_provider().rates(postal_code, lines, area_id,
                origin_postal_code=origin["postal_code"], origin_area_id=origin.get("area_id", ""))
    except ProviderError as error:
        if "sufficient balance" not in str(error).lower():
            logger.warning("shipping rates failed for %s: %s", request.store.slug, error)
            return JsonResponse({"error": _shopper_shipping_error(error)}, status=502)
        logger.warning("Biteship balance is empty; store %s falls back to its own rates", request.store.slug)
        methods = ShippingMethod.objects.filter(store=request.store, is_active=True)
        stored, response_quotes = {}, []
        for method in methods:
            token = uuid.uuid4().hex
            price = int(method.quote(subtotal))
            stored[token] = {"provider": "demo", "courier_code": method.code,
                "service_code": method.code, "name": method.name, "price": price,
                "eta": method.eta, "postal_code": postal_code, "destination_area_id": area_id,
                "cart_signature": _cart_signature(lines)}
            response_quotes.append({"token": token, "name": method.name,
                "price": price, "eta": method.eta})
        if not response_quotes: return JsonResponse({"error": str(error)}, status=402)
        request.session["shipping_quotes"] = stored
        return JsonResponse({"quotes": response_quotes, "fallback": True,
            "provider_name": _shipping_provider_name(),
            "warning": f"Ongkir ditetapkan oleh {request.store.name}."})
    stored = {}
    response_quotes = []
    for quote in quotes:
        token = uuid.uuid4().hex
        stored[token] = {"provider": quote.provider, "courier_code": quote.courier_code,
            "service_code": quote.service_code, "name": quote.name, "price": quote.price, "eta": quote.eta,
            "postal_code": postal_code, "destination_area_id": area_id,
            "cart_signature": _cart_signature(lines)}
        response_quotes.append({"token": token, "name": quote.name, "price": quote.price,
            "eta": quote.eta, "courier_code": quote.courier_code,
            "service_code": quote.service_code})
    request.session["shipping_quotes"] = stored
    response_quotes = present_quotes(response_quotes)
    return JsonResponse({"quotes": response_quotes, "provider_name": _shipping_provider_name(),
        "destination_area_id": area_id, "postal_code": postal_code})


def _cart_reply(request, ok, message, fallback):
    """Quick add from the storefront answers the page's script with JSON; a plain form post
    is redirected as before."""
    if request.headers.get("X-Requested-With") == "fetch":
        lines, subtotal = get_cart(request)
        return JsonResponse({"ok": ok, "message": message,
            "count": sum(line["quantity"] for line in lines), "total": int(subtotal)},
            status=200 if ok else 400)
    (messages.success if ok else messages.error)(request, message)
    return fallback


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
            return _cart_reply(request, False, "Pilih varian produk terlebih dahulu.",
                redirect("product-detail", slug=product.slug))
    key = f"v:{variant.id}" if variant else f"p:{product.id}"
    available = orderable_quantity(product, variant)
    if available <= 0:
        return _cart_reply(request, False, f"Stok {product.name} sedang habis.",
            redirect("product-detail", slug=product.slug))
    try: requested = max(1, int(request.POST.get("quantity", 1)))
    except ValueError: requested = 1
    try: in_cart = max(0, int(cart.get(key, 0)))
    except (TypeError, ValueError): in_cart = 0
    cart[key] = min(in_cart + requested, available)
    request.session["cart"] = cart
    _save_note(request, key)
    lines, subtotal = get_cart(request)
    update_cart_snapshot(request, lines, subtotal)
    record_event(request, "cart_add", product=product, metadata={"quantity": requested}, always=True)
    return _cart_reply(request, True, f"{product.name} ditambahkan ke keranjang.",
        redirect("checkout" if request.POST.get("next") == "checkout" else "cart"))

def _save_note(request, key):
    if "note" not in request.POST:
        return
    notes = dict(request.session.get("cart_notes", {}))
    note = request.POST.get("note", "").strip()[:200]
    if note:
        notes[key] = note
    else:
        notes.pop(key, None)
    request.session["cart_notes"] = notes


def cart_update(request, item_key):
    if request.method != "POST" or not request.store: raise Http404
    kind, _, raw_id = item_key.partition(":")
    if not raw_id: kind, raw_id = "p", kind
    if kind == "v":
        variant = get_object_or_404(ProductVariant.objects.select_related("product"), id=raw_id,
            product__store=request.store, product__is_active=True, is_active=True)
        product = variant.product
        available = orderable_quantity(product, variant)
    else:
        product = get_object_or_404(Product.objects.prefetch_related("inventory_levels"), id=raw_id, store=request.store)
        available = orderable_quantity(product)
    try: quantity = max(0, min(int(request.POST.get("quantity", 1)), available))
    except ValueError: quantity = 1
    cart = request.session.get("cart", {})
    if quantity: cart[item_key] = quantity
    else: cart.pop(item_key, None)
    request.session["cart"] = cart
    _save_note(request, item_key)
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
    context = {"lines": lines, "subtotal": subtotal, "recovery_session": commerce_session(request)}
    if lines and needs_schedule(request.store, lines):
        # Offer every date with at least one left, and say when the cart wants more than that,
        # rather than hiding a date the shopper may prefer.
        dates = available_dates(request.store, [{**line, "quantity": 1} for line in lines])
        chosen = request.GET.get("date") or request.session.get("delivery_date", "")
        day = next((d for d in dates if d.isoformat() == chosen), dates[0] if dates else None)
        if day:
            request.session["delivery_date"] = day.isoformat()
        for line in lines:
            if line["product"].is_preorder and day:
                line["remaining"] = preorder_remaining(line["product"], day)
                line["over_quota"] = (line["remaining"] is not None and
                    line["quantity"] > line["remaining"])
        context.update(delivery_dates=dates, delivery_day=day,
            schedule_blocked=any(line.get("over_quota") for line in lines))
    return render(request, "storefront/cart.html", context)

def checkout(request):
    """Guests can check out with a name and WhatsApp number; an account is optional."""
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
    # A cart of only digital products needs no address and no courier.
    digital_only = all(line["product"].is_digital for line in lines)
    biteship_enabled = _shipping_provider_enabled(request.store) and not digital_only
    shipping_methods = [] if biteship_enabled else list(ShippingMethod.objects.filter(store=request.store, is_active=True))
    if digital_only:
        shipping_methods = [ShippingMethod(store=request.store, code="digital",
            name="Produk digital (diunduh)", provider="digital", flat_rate=0)]
    requested_shipping = request.POST.get("shipping_method", "")
    selected_shipping = next((method for method in shipping_methods if method.code == requested_shipping), None)
    live_quote = request.session.get("shipping_quotes", {}).get(requested_shipping) if biteship_enabled else None
    if not selected_shipping and shipping_methods and (not requested_shipping or request.method != "POST"):
        selected_shipping = shipping_methods[0]
    if not shipping_methods and not biteship_enabled:
        selected_shipping = ShippingMethod(store=request.store, code="pickup", name="Ambil di toko", flat_rate=0)
        shipping_methods = [selected_shipping]
    pickup_outlets = [] if digital_only else list(Warehouse.objects.filter(store=request.store,
        is_outlet=True, is_active=True, pickup_enabled=True))
    pickup_outlet = next((outlet for outlet in pickup_outlets
        if requested_shipping == f"outlet-{outlet.id}"), None)
    if pickup_outlet:
        # Collected by the shopper: free, and stock comes from that outlet.
        selected_shipping = ShippingMethod(store=request.store, code=f"outlet-{pickup_outlet.id}",
            name=f"Ambil di {pickup_outlet.name}", provider="pickup", flat_rate=0,
            eta=pickup_outlet.opening_hours)
        live_quote = None
    shipping_cost = Decimal(live_quote["price"]) if live_quote else (selected_shipping.quote(subtotal) if selected_shipping else Decimal(0))
    coupon = get_coupon(request.store, request.POST.get("coupon_code", ""), subtotal) if request.method == "POST" else None
    base_price = calculate_price(store=request.store, lines=lines, shipping_cost=shipping_cost, coupon=coupon)
    loyalty_points, loyalty_discount = redemption_quote(request.store, request.user,
        request.POST.get("loyalty_points", ""), base_price.subtotal - base_price.discount)
    price = calculate_price(store=request.store, lines=lines, shipping_cost=shipping_cost, coupon=coupon,
        additional_discount=loyalty_discount)
    gift_code = request.POST.get("gift_card_code", "").strip() if request.method == "POST" else ""
    gift_amount = gift_card_usable(find_gift_card(request.store, gift_code),
        price.subtotal - price.discount, price.shipping) if gift_code else Decimal(0)
    if gift_amount:
        price = calculate_price(store=request.store, lines=lines, shipping_cost=shipping_cost,
            coupon=coupon, additional_discount=loyalty_discount + gift_amount)
    discount, shipping_cost = price.discount, price.shipping
    payment_choices = payment_options(request.store)
    payment_choice = next((option for option in payment_choices
        if option.code == request.POST.get("payment_method")), payment_choices[0] if payment_choices else None)
    schedule_needed = needs_schedule(request.store, lines)
    schedule_error = ""
    if request.method == "POST":
        try:
            validate_schedule(request.store, lines, request.POST.get("delivery_date"),
                request.POST.get("delivery_slot"))
        except ScheduleError as error:
            schedule_error = str(error)
        required = ["customer_name", "phone"] + ([] if pickup_outlet or digital_only else ["shipping_address"])
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
        elif not payment_choices:
            messages.error(request, "Toko ini belum mengaktifkan metode pembayaran.")
        elif schedule_error:
            messages.error(request, schedule_error)
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
                    # The coupon row lock serializes this count against concurrent checkouts.
                    if coupon.per_customer_limit is not None and coupon_uses_by_customer(
                            coupon, user=request.user, phone=phone) >= coupon.per_customer_limit:
                        messages.error(request, "Voucher ini sudah mencapai batas pemakaian untuk akun Anda.")
                        return redirect("checkout")
                base_price = calculate_price(store=request.store, lines=lines,
                    shipping_cost=shipping_cost, coupon=coupon)
                locked_loyalty = (loyalty_account(request.store, request.user, lock=True)
                    if request.user.is_authenticated else None)
                loyalty_points, loyalty_discount = redemption_quote(request.store, request.user,
                    request.POST.get("loyalty_points", ""), base_price.subtotal - base_price.discount,
                    account=locked_loyalty)
                price = calculate_price(store=request.store, lines=lines,
                    shipping_cost=shipping_cost, coupon=coupon, additional_discount=loyalty_discount)
                locked_card = find_gift_card(request.store, gift_code, lock=True) if gift_code else None
                if gift_code and locked_card is None:
                    messages.error(request, "Kode gift card tidak berlaku atau saldonya habis.")
                    return redirect("checkout")
                gift_amount = gift_card_usable(locked_card, price.subtotal - price.discount,
                    price.shipping)
                if gift_amount:
                    price = calculate_price(store=request.store, lines=lines,
                        shipping_cost=shipping_cost, coupon=coupon,
                        additional_discount=loyalty_discount + gift_amount)
                product_ids = [x["product"].id for x in lines if not x["variant"]]
                variant_ids = [x["variant"].id for x in lines if x["variant"]]
                locked = {p.id: p for p in Product.objects.select_for_update().filter(id__in=product_ids)}
                locked_variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(
                    id__in=variant_ids, product__store=request.store, is_active=True,
                ).select_related("product")}
                levels = list(InventoryLevel.objects.select_for_update().filter(
                    product_id__in=product_ids, warehouse__store=request.store, warehouse__is_active=True,
                ).select_related("warehouse").order_by("warehouse__priority", "warehouse_id"))
                if pickup_outlet:
                    levels = [level for level in levels if level.warehouse_id == pickup_outlet.id]
                by_product = {product_id: [] for product_id in product_ids}
                for level in levels: by_product[level.product_id].append(level)
                insufficient = any(
                    (locked_variants.get(x["variant"].id).available_stock if locked_variants.get(x["variant"].id) else 0) < x["quantity"]
                    if x["variant"] else sum(level.available for level in by_product[x["product"].id]) < x["quantity"]
                    for x in lines if not x["product"].is_preorder and not x["product"].is_digital
                )
                if insufficient:
                    messages.error(request, f"Stok di {pickup_outlet.name} tidak mencukupi. Pilih "
                        "outlet lain atau pengiriman." if pickup_outlet else
                        "Stok berubah. Silakan periksa keranjang.")
                    return redirect("checkout" if pickup_outlet else "cart")
                try:
                    delivery_date, delivery_slot = validate_schedule(request.store, lines,
                        request.POST.get("delivery_date"), request.POST.get("delivery_slot"),
                        lock=True)
                except ScheduleError as error:
                    messages.error(request, str(error))
                    return redirect("checkout")
                order = Order.objects.create(store=request.store,
                    customer_user=request.user if request.user.is_authenticated else None,
                    subtotal=price.subtotal, tax=price.tax,
                    shipping_cost=price.shipping, discount=price.discount, total=price.total,
                    loyalty_discount=loyalty_discount, loyalty_points_redeemed=loyalty_points,
                    gift_card=locked_card if gift_amount else None, gift_card_amount=gift_amount,
                    customer_name=request.POST["customer_name"].strip(), email=request.POST.get("email", "").strip(),
                    phone=phone, shipping_address=(request.POST.get("shipping_address", "").strip()
                        or ("Produk digital" if digital_only else
                            f"Ambil di {pickup_outlet.name}: {pickup_outlet.address}".strip(": "))),
                    pickup_outlet=pickup_outlet,
                    campaign_link_id=attributed_campaign_id(request),
                    province=request.POST.get("province", "").strip(), city=request.POST.get("city", "").strip(),
                    district=request.POST.get("district", "").strip(), village=request.POST.get("village", "").strip(),
                    postal_code=request.POST.get("postal_code", "").strip(), delivery_notes=request.POST.get("delivery_notes", "").strip(),
                    destination_area_id=(live_quote["destination_area_id"] if live_quote else
                        request.POST.get("destination_area_id", "").strip()),
                    payment_provider=payment_choice.provider, payment_method=payment_choice.code,
                    paid_to_store_account=payment_choice.store_account,
                    shipping_provider=live_quote["provider"] if live_quote else selected_shipping.provider,
                    courier_code=live_quote["courier_code"] if live_quote else selected_shipping.code,
                    courier_service_code=live_quote["service_code"] if live_quote else "",
                    payment_expires_at=timezone.now() + PAYMENT_SESSION_CREATION_WINDOW,
                    shipping_service=live_quote["name"] if live_quote else selected_shipping.name,
                    shipping_eta=live_quote["eta"] if live_quote else selected_shipping.eta,
                    coupon_code=coupon.code if coupon else "", idempotency_key=checkout_key,
                    delivery_date=delivery_date, delivery_slot=delivery_slot,
                    delivery_slot_label=delivery_slot.label if delivery_slot else "")
                redeem_for_order(order, loyalty_points)
                redeem_gift_card(locked_card, order, gift_amount)
                for line in lines:
                    variant = locked_variants.get(line["variant"].id) if line["variant"] else None
                    product = variant.product if variant else locked[line["product"].id]
                    item = OrderItem.objects.create(order=order, product=product, variant=variant,
                        name=product.name, sku=variant.sku if variant else product.sku,
                        variant_name=variant.name if variant else "", flash_sale_offer=line["sale_offer"],
                        unit_price=line["unit_price"],
                        quantity=line["quantity"], line_total=line["total"],
                        is_preorder=product.is_preorder, note=line.get("note", ""))
                    if product.is_preorder or product.is_digital:
                        continue  # made to order (quota checked with the date) or downloaded
                    if variant:
                        reserve_variant(item, variant, line["quantity"])
                        continue
                    reserve_from_levels(item, by_product[product.id], line["quantity"])
                provider = order_payment_provider(order)
                order.payment_reference = reserved_reference(provider, order)
                order.save(update_fields=["payment_reference"])
                Customer.objects.update_or_create(store=request.store, phone=phone, defaults={"name": order.customer_name,
                    "email": order.email, "accepts_marketing": request.POST.get("accepts_marketing") == "on"})
                OrderStatusEvent.objects.create(order=order, status="pending", note="Pesanan dibuat")
                if request.POST.get("save_address") == "on" and request.user.is_authenticated:
                    save_checkout_address(request.store, request.user, request.POST, phone)
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
            request.session.pop("cart_notes", None)
            request.session.pop("delivery_date", None)
            request.session.pop("checkout_draft", None)
            request.session["checkout_key"] = uuid.uuid4().hex
            record_event(request, "converted", metadata={"order_id": str(order.id),
                "total": str(order.total)}, always=True)
            update_cart_snapshot(request, [], 0)
            return redirect(payment.checkout_url)
    loyalty = (loyalty_account(request.store, request.user)
        if request.store.loyalty_enabled and request.user.is_authenticated else None)
    account_name = request.user.get_full_name() if request.user.is_authenticated else ""
    account_email = request.user.email if request.user.is_authenticated else ""
    addresses = saved_addresses(request.store, request.user)
    saved = {"email": "", "coupon_code": ""} | checkout_prefill(addresses,
        posted=request.method == "POST")
    draft = request.session.get("checkout_draft", {})
    if request.method != "POST":
        saved |= {name: value for name, value in draft.items()
            if name in CHECKOUT_DRAFT_FIELDS and value}
    initial_step = 3 if request.method == "POST" else draft.get("step", 1)
    return render(request, "storefront/checkout.html", {"lines": lines, "subtotal": subtotal, "digital_only": digital_only,
        "shipping_methods": shipping_methods, "shipping_cost": shipping_cost, "discount": discount,
        "idempotency_key": checkout_key,
        "biteship_enabled": biteship_enabled,
        "biteship_configured": biteship_enabled,
        "shipping_provider_name": _shipping_provider_name(), "account_name": account_name,
        "account_email": account_email, "loyalty_account": loyalty,
        "loyalty_discount": loyalty_discount, "loyalty_points": loyalty_points,
        "addresses": addresses, "saved": saved, "initial_step": initial_step,
        "payment_choices": payment_choices, "payment_choice": payment_choice,
        "schedule_needed": schedule_needed, "pickup_outlets": pickup_outlets,
        "pickup_outlet": pickup_outlet,
        "delivery_dates": available_dates(request.store, lines) if schedule_needed else [],
        "chosen_date": request.POST.get("delivery_date") or request.session.get("delivery_date", ""),
        "delivery_slots": slot_options(request.store, None) if schedule_needed else [],
        "has_preorder": any(line["product"].is_preorder for line in lines),
        "trust": store_trust(request.store)})


# What the stepped checkout remembers between steps and visits (personal details stay in the
# server-side session, never in the browser's storage).
CHECKOUT_DRAFT_FIELDS = CustomerAddress.CHECKOUT_FIELDS + ("email", "coupon_code")


@require_POST
def checkout_draft(request):
    """Save the checkout form as the shopper moves between steps, so a reload or a later visit
    picks up where they left off."""
    if not request.store:
        raise Http404
    draft = {name: request.POST.get(name, "").strip()[:500] for name in CHECKOUT_DRAFT_FIELDS
        if name in request.POST}
    try:
        draft["step"] = min(3, max(1, int(request.POST.get("step", 1))))
    except ValueError:
        draft["step"] = 1
    request.session["checkout_draft"] = {**request.session.get("checkout_draft", {}), **draft}
    return JsonResponse({"saved": True})


def delivery_slots(request):
    """Slots and their remaining places for a date, for the checkout date picker."""
    if not request.store:
        raise Http404
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse({"slots": []})
    return JsonResponse({"slots": [{"id": option["slot"].id, "label": option["slot"].label,
        "remaining": option["remaining"], "full": option["full"]}
        for option in slot_options(request.store, day)]})


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
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Akun berhasil dibuat. Selamat berbelanja!")
        return redirect(next_url or "account-orders")
    return render(request, "registration/register.html", {"form": form, "next": next_url})


def account_orders(request):
    """Pesanan Saya: status tabs, estimated arrival and next step for every order. Guests see
    the orders this browser placed."""
    if not request.store:
        raise Http404
    orders = buyer_orders(request)
    counts = dict(orders.values_list("status").annotate(total=Count("id")))
    tabs = [{"key": key, "label": label, "count": sum(counts.get(status, 0) for status in statuses)}
        for key, label, statuses in BUYER_TABS]
    tab = request.GET.get("tab", "")
    groups = {key: statuses for key, _, statuses in BUYER_TABS}
    if tab in groups:
        orders = orders.filter(status__in=groups[tab])
    page, page_range, page_query = paginate(request, orders.prefetch_related(
        "items__product", "status_events").order_by("-created_at"), 20)
    reviewed = set(ProductReview.objects.filter(order__in=[order.id for order in page.object_list])
        .values_list("order_id", "product_id"))
    for order in page.object_list:
        order.needs_review = order.status in REVIEWABLE_STATUSES and any(
            (order.id, item.product_id) not in reviewed for item in order.items.all())
        order.eta = estimated_arrival(order)
        order.first_item = next(iter(order.items.all()), None)
        order.more_items = max(0, len(order.items.all()) - 1)
        order.parcel = courier_tracking(order)
    loyalty = (LoyaltyAccount.objects.filter(store=request.store, user=request.user)
        .prefetch_related("transactions").first() if request.user.is_authenticated else None)
    return render(request, "storefront/account_orders.html", {"orders": page.object_list,
        "page_obj": page, "page_range": page_range, "page_query": page_query, "tabs": tabs,
        "tab": tab, "total": sum(counts.values()), "loyalty_account": loyalty})


def order_received(request, order_id):
    """The shopper confirms the parcel arrived, which completes the order."""
    if request.method != "POST" or not request.store:
        raise Http404
    order = get_customer_order_or_404(request, order_id)
    if order.status not in {"shipped", "delivered"}:
        messages.error(request, "Pesanan ini belum dikirim.")
        return redirect("order-detail", order_id=order.id)
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(id=order.id)
        advance_order(locked, "completed", note="Dikonfirmasi diterima oleh pembeli")
    messages.success(request, "Terima kasih! Pesanan selesai.")
    return redirect("order-detail", order_id=order.id)


def order_buy_again(request, order_id):
    """Put the items of a past order back in the cart, as far as stock allows."""
    if request.method != "POST" or not request.store:
        raise Http404
    order = get_customer_order_or_404(request, order_id, queryset=Order.objects.prefetch_related(
        "items__product", "items__variant"))
    cart, added = request.session.get("cart", {}), 0
    for item in order.items.all():
        if not item.product.is_active:
            continue
        variant = item.variant if item.variant and item.variant.is_active else None
        if item.variant_id and variant is None:
            continue
        key = f"v:{variant.id}" if variant else f"p:{item.product_id}"
        available = variant.available_stock if variant else item.product.available_stock
        quantity = min(int(cart.get(key, 0)) + item.quantity, available)
        if quantity > 0:
            cart[key] = quantity
            added += 1
    request.session["cart"] = cart
    if added:
        messages.success(request, f"{added} produk dimasukkan lagi ke keranjang.")
    else:
        messages.error(request, "Produk dari pesanan ini sedang tidak tersedia.")
    return redirect("cart")

@login_required(login_url="/account/login/")
def account_addresses(request, address_id=None):
    if not request.store:
        raise Http404
    addresses = CustomerAddress.objects.filter(store=request.store, user=request.user)
    instance = get_object_or_404(addresses, id=address_id) if address_id else None
    form = CustomerAddressForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        address = form.save(commit=False)
        address.store, address.user = request.store, request.user
        make_default = address.is_default or not addresses.exclude(id=address.id).exists()
        address.is_default = False
        address.save()
        if make_default:
            address.make_default()
        messages.success(request, "Alamat tersimpan.")
        return redirect("account-addresses")
    return render(request, "storefront/addresses.html", {"addresses": addresses, "form": form,
        "editing": instance})


@login_required(login_url="/account/login/")
def account_address_action(request, address_id, action):
    if request.method != "POST" or not request.store or action not in {"delete", "default"}:
        raise Http404
    address = get_object_or_404(CustomerAddress, id=address_id, store=request.store, user=request.user)
    if action == "default":
        address.make_default()
    else:
        was_default = address.is_default
        address.delete()
        replacement = CustomerAddress.objects.filter(store=request.store, user=request.user).first()
        if was_default and replacement:
            replacement.make_default()
    return redirect("account-addresses")


def order_detail(request, order_id):
    if not request.store: raise Http404
    order = get_customer_order_or_404(
        request, order_id, queryset=Order.objects.prefetch_related(
            "items", "status_events", "tracking_events")
    )
    if request.GET.get("token"):
        remember_customer_order(request, order)  # a payment link: keep it on this device
    from .manual_views import DOWNLOADABLE
    downloads = [item for item in order.items.all() if item.product.is_digital]
    return render(request, "storefront/order.html", {"order": order,
        "downloads": downloads, "can_download": order.status in DOWNLOADABLE,
        "eta": estimated_arrival(order), "steps": tracker_steps(order),
        "order_code": order_code(order), "parcel": courier_tracking(order),
        "review_items": order_review_items(order), "review_form": OrderReviewForm()})

def track_order(request):
    """Lacak pesanan: a guest finds an order by its number and WhatsApp number, on any device."""
    if not request.store:
        raise Http404
    error = ""
    if request.method == "POST":
        try:
            phone = normalize_indonesian_phone(request.POST.get("phone", ""))
        except ValueError:
            phone = ""
        order = phone and find_guest_order(request.store, request.POST.get("code", ""), phone)
        if order:
            remember_customer_order(request, order)
            return redirect("order-detail", order_id=order.id)
        error = "Pesanan tidak ditemukan. Cek lagi nomor pesanan dan nomor WhatsApp-nya."
    return render(request, "storefront/track_order.html", {"error": error,
        "code": request.POST.get("code", ""), "phone": request.POST.get("phone", "")})


def order_review(request, order_id):
    """Penilaian produk from the order page, for guests and account holders alike: the order
    itself proves the purchase."""
    if request.method != "POST" or not request.store:
        raise Http404
    order = get_customer_order_or_404(request, order_id)
    entry = next((entry for entry in order_review_items(order)
        if str(entry["product"].id) == request.POST.get("product")), None)
    if entry is None:
        messages.error(request, "Penilaian dapat diberikan setelah pesanan diterima.")
        return redirect("order-detail", order_id=order.id)
    form = OrderReviewForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(error for errors in form.errors.values()
            for error in errors))
        return redirect(reverse("order-detail", args=[order.id]) + f"#ulasan-{entry['product'].id}")
    save_order_review(order, entry["item"], rating=form.cleaned_data["rating"],
        title=form.cleaned_data["title"].strip(), body=form.cleaned_data["body"].strip(),
        photos=form.cleaned_data["photos"], user=request.user)
    messages.success(request, f"Terima kasih! Penilaian untuk {entry['product'].name} sudah tampil "
        "di halaman produk.")
    return redirect(reverse("order-detail", args=[order.id]) + "#ulasan")


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
    return {"merchant_store": store, "merchant_stores": stores, "merchant_role": role,
        "merchant_store_url": store_url(store), "merchant_preview_url": preview_url(store),
        **context}


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
# Smallest DOKU Lakuin balance a seller can ask to have transferred.
MIN_PLATFORM_PAYOUT = Decimal(10000)


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
    setup = setup_steps(store)
    setup_done = sum(step.done for step in setup)
    live_state = store_state(store)
    loyalty = LoyaltyAccount.objects.filter(store=store).aggregate(
        members=Count("id"), outstanding=Sum("balance"), earned=Sum("lifetime_earned"))
    return render(request, "merchant/dashboard.html", _merchant_context(request, store,
        live_state=live_state, revenue=revenue, order_count=order_count, average_order=(revenue / order_count if order_count else 0),
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
        engagement=engagement, loyalty=loyalty, setup=setup, setup_done=setup_done,
        subscription_status=subscription_status(store),
        to_ship=sum(fulfillment.values()), problem_count=payment_errors + reconciliation_mismatches
            + failed_integrations + shipping_problems,
        transfers_to_confirm=store.orders.filter(status="pending",
            payment_provider="bank_transfer").count(),
        month_revenue=sum((row["revenue"] for row in daily), Decimal(0))))

@login_required(login_url="/merchant/login/")
def merchant_orders(request):
    store = _staff_store(request)
    all_orders = Order.objects.filter(store=store)
    counts = dict(all_orders.values_list("status").annotate(total=Count("id")))
    tabs = [{"key": key, "label": label, "count": sum(counts.get(status, 0) for status in statuses)}
        for key, label, statuses in ORDER_GROUPS]
    group = request.GET.get("tab")
    if group not in {tab["key"] for tab in tabs} | {"all"}:
        # Open where the work is: payments to check, then orders to pack, then to ship.
        group = next((tab["key"] for tab in tabs[:3] if tab["count"]), "all")
    orders = all_orders.select_related("pickup_outlet").prefetch_related("items",
        "tracking_events").order_by("-created_at")
    if group != "all":
        orders = orders.filter(status__in={key: statuses for key, _, statuses in ORDER_GROUPS}[group])
    status = request.GET.get("status")
    if status:
        orders = orders.filter(status=status)
    try:
        orders = orders.filter(delivery_date=date.fromisoformat(request.GET.get("date", "")))
    except ValueError:
        pass
    outlet = request.GET.get("outlet", "")
    if outlet.isdigit():
        orders = orders.filter(pickup_outlet_id=int(outlet))
    if group in {"pack", "ship"}:
        # Scheduled orders first, by the day they must leave.
        orders = orders.order_by(F("delivery_date").asc(nulls_last=True), "created_at")
    query = request.GET.get("q", "").strip()
    if query:
        matches = (models.Q(customer_name__icontains=query) | models.Q(phone__icontains=query) |
            models.Q(tracking_number__icontains=query) |
            models.Q(payment_reference__icontains=query))
        if re.fullmatch(r"[0-9a-fA-F]{4,8}", query):  # the short #id shown on each order
            matches |= models.Q(id__startswith=query.lower())
        orders = orders.filter(matches)
    page, page_range, page_query = paginate(request, orders, MERCHANT_PAGE_SIZE)
    for order in page.object_list:
        order.available_statuses = allowed_order_transitions(order)
        order.can_book_shipment = can_book_shipment(order)
        order.courier_label = has_courier_label(order)
        order.courier_name = courier_name(order.courier_code)
        order.booking_pending = is_booking_pending(order)
        parcel = courier_tracking(order)
        order.whatsapp_url = _whatsapp_link(order.phone,
            f"Halo {order.customer_name}, terima kasih sudah berbelanja di {store.name}. "
            f"Pesanan #{str(order.id)[:8]}" + (f" sudah dikirim dengan {parcel['name']}, "
                f"nomor resi {parcel['number']}." + (f" Lacak di {parcel['url']}" if parcel["url"]
                else "") if parcel else ""))
    return render(request, "merchant/orders.html", _merchant_context(request, store,
        orders=page.object_list, page_obj=page, page_range=page_range, page_query=page_query,
        tabs=tabs, tab=group, query=query, total=all_orders.count(),
        outlets=Warehouse.objects.filter(store=store, is_outlet=True),
        statuses=Order.STATUS, fulfillment_methods=Order.FULFILLMENT_METHODS,
        tracking_sync=bool(settings.BITESHIP_API_KEY or settings.RAJAONGKIR_API_KEY)))


def _whatsapp_link(phone, text):
    digits = "".join(character for character in phone if character.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return f"https://wa.me/{digits}?text={url_quote(text)}" if len(digits) >= 9 else ""


def _back_to_orders(request):
    """Return to the tab and page the seller acted from."""
    target = request.POST.get("next", "")
    if target.startswith("/merchant/orders/") and url_has_allowed_host_and_scheme(target,
            allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("merchant-orders")


FULFILMENT_ROLES = {"admin", "manager", "ops", "warehouse"}


@login_required(login_url="/merchant/login/")
def merchant_order_advance(request, order_id):
    """One-click steps: pack (through confirmed, allocated and picking), received, completed."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    target = request.POST.get("to", "")
    if target not in {"packed", "delivered", "completed"}:
        raise Http404
    _require_roles(request, store, FULFILMENT_ROLES if target == "packed" else
        FULFILMENT_ROLES | {"customer_service"})
    labels = {"packed": "dikemas dan siap dikirim", "delivered": "ditandai sudah diterima",
        "completed": "diselesaikan"}
    try:
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update(), id=order_id, store=store)
            old_status = order.status
            advance_order(order, target, note=request.POST.get("note", "").strip(),
                actor=request.user)
            AuditLog.objects.create(store=store, actor=request.user, action="order.status_updated",
                object_type="Order", object_id=str(order.id),
                details={"from": old_status, "to": target})
    except InvalidOrderTransition as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Pesanan #{str(order.id)[:8]} {labels[target]}.")
    return _back_to_orders(request)


def _apply_tracking(order, data):
    """Copy a posted courier and tracking number onto the order. Returns an error, or ""."""
    tracking_number = data.get("tracking_number", "").strip()[:100]
    courier_code = data.get("courier_code", "").strip().lower()[:40]
    method = data.get("fulfillment_method", order.fulfillment_method or "merchant_pickup")
    if method not in dict(Order.FULFILLMENT_METHODS):
        return "Metode fulfillment tidak valid."
    if not tracking_number or not re.fullmatch(r"[A-Za-z0-9._/-]+", tracking_number):
        return "Masukkan nomor resi yang valid."
    if not courier_code or not re.fullmatch(r"[a-z0-9_-]+", courier_code):
        return "Masukkan kode kurir, misalnya jne, jnt, sicepat, atau anteraja."
    old_tracking = order.tracking_number
    order.tracking_number, order.courier_code, order.fulfillment_method = (tracking_number,
        courier_code, method)
    order.tracking_status = "ready_to_track"
    order.tracking_status_detail = "Nomor resi tersimpan."
    order.tracking_error = ""
    order.tracking_last_checked_at = None
    order.save(update_fields=["tracking_number", "courier_code", "fulfillment_method",
        "tracking_status", "tracking_status_detail", "tracking_error",
        "tracking_last_checked_at"])
    if old_tracking and old_tracking != tracking_number:
        order.tracking_events.all().delete()
    return ""


@login_required(login_url="/merchant/login/")
def merchant_order_ship(request, order_id):
    """The seller handed the parcel to a courier: save the tracking number and mark it shipped,
    packing it first if that was not done yet."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, FULFILMENT_ROLES)
    try:
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update(), id=order_id, store=store)
            old_status = order.status
            if order.status not in FULFILMENT_PATH[:FULFILMENT_PATH.index("shipped")]:
                raise InvalidOrderTransition("Pesanan ini tidak sedang menunggu dikirim.")
            error = _apply_tracking(order, request.POST)
            if error:
                raise InvalidOrderTransition(error)
            advance_order(order, "shipped", note=f"Dikirim · {order.courier_code.upper()} "
                f"{order.tracking_number}", actor=request.user)
            AuditLog.objects.create(store=store, actor=request.user, action="order.shipped",
                object_type="Order", object_id=str(order.id), details={"from": old_status,
                    "courier_code": order.courier_code, "tracking_number": order.tracking_number})
    except InvalidOrderTransition as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Pesanan #{str(order.id)[:8]} dikirim dengan resi "
            f"{order.tracking_number}.")
    return _back_to_orders(request)


LABEL_LIMIT = 100
# Paper sizes for the label page; thermal 100 x 150 mm is the marketplace standard.
LABEL_FORMATS = {"100x150": "Thermal 100×150 mm", "100x100": "Thermal 100×100 mm",
    "a6": "A6", "a4": "A4 (4 label per halaman)"}


@login_required(login_url="/merchant/login/")
def merchant_order_courier_label(request, order_id):
    """Open the courier's official label (JNE, J&T, SiCepat, …) for an order booked through
    Biteship; print it at 100% on 10×15 cm thermal paper."""
    store = _staff_store(request)
    _require_roles(request, store, FULFILMENT_ROLES | {"customer_service"})
    order = get_object_or_404(Order, id=order_id, store=store)
    if not has_courier_label(order):
        return redirect(f"{reverse('merchant-order-labels')}?id={order.id}&print=1")
    try:
        url = courier_label_url(order)
    except ProviderError as error:
        logger.warning("courier label for %s failed: %s", order.id, error)
        messages.error(request, "Label resmi kurir belum tersedia. Coba lagi sebentar, atau cetak "
            "label Lakuin.")
        return redirect("merchant-orders")
    Order.objects.filter(id=order.id, label_printed_at__isnull=True).update(
        label_printed_at=timezone.now())
    return redirect(url)


@login_required(login_url="/merchant/login/")
def merchant_order_courier_labels(request):
    """The official courier labels of the ticked orders (?id=…&id=…) as one PDF to print in one
    go. Orders without a booked courier keep the Lakuin label."""
    store = _staff_store(request)
    _require_roles(request, store, FULFILMENT_ROLES | {"customer_service"})
    try:
        ids = [uuid.UUID(value) for value in request.GET.getlist("id")[:LABEL_LIMIT]]
    except ValueError as error:
        raise Http404 from error
    orders = [order for order in Order.objects.filter(store=store, id__in=ids).order_by("created_at")
        if has_courier_label(order)]
    if not orders:
        messages.error(request, "Belum ada pesanan dengan kurir yang sudah dipesan.")
        return redirect("merchant-orders")
    pdf, missing = merged_courier_labels(orders)
    if pdf is None:
        messages.error(request, "Label resmi kurir belum tersedia. Coba lagi sebentar, atau cetak "
            "label Lakuin.")
        return redirect("merchant-orders")
    Order.objects.filter(id__in=[order.id for order in orders if order not in missing],
        label_printed_at__isnull=True).update(label_printed_at=timezone.now())
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="label-kurir-{timezone.localdate():%Y%m%d}.pdf"')
    if missing:
        response["X-Labels-Missing"] = ",".join(str(order.id) for order in missing)
    return response


@login_required(login_url="/merchant/login/")
def merchant_order_labels(request):
    """Printable shipping labels like the marketplaces': ticked orders (?id=…&id=…), or a whole
    tab (?tab=). Opening the page marks the labels as printed."""
    store = _staff_store(request)
    _require_roles(request, store, FULFILMENT_ROLES | {"customer_service"})
    orders = Order.objects.filter(store=store).prefetch_related("items__product").order_by(
        "created_at")
    ids = request.GET.getlist("id")
    groups = {key: statuses for key, _, statuses in ORDER_GROUPS}
    if ids:
        try:
            orders = orders.filter(id__in=[uuid.UUID(value) for value in ids[:LABEL_LIMIT]])
        except ValueError as error:
            raise Http404 from error
    else:
        orders = orders.filter(status__in=groups.get(request.GET.get("tab"), groups["pack"]))
    orders = list(orders[:LABEL_LIMIT])
    if len(orders) == 1 and len(ids) == 1 and has_courier_label(orders[0]):
        # One order the courier was booked for: print the courier's own label.
        return redirect("merchant-order-courier-label", order_id=orders[0].id)
    if len(orders) > 1 and ids and all(has_courier_label(order) for order in orders):
        # Every ticked order was booked: one PDF of the couriers' own labels.
        return redirect(f"{reverse('merchant-order-courier-labels')}?"
            + "&".join(f"id={order.id}" for order in orders))
    for order in orders:
        order.courier_label = has_courier_label(order)
        order.weight_grams = sum((item.product.weight_grams or 0) * item.quantity
            for item in order.items.all())
        order.weight_kg = Decimal(order.weight_grams) / 1000
        order.is_cod = order.payment_method == "cod"
    Order.objects.filter(id__in=[order.id for order in orders],
        label_printed_at__isnull=True).update(label_printed_at=timezone.now())
    paper = request.GET.get("format") if request.GET.get("format") in LABEL_FORMATS else "100x150"
    query = request.GET.copy()
    query.pop("format", None)
    query.pop("print", None)
    return render(request, "merchant/order_labels.html", {"store": store, "orders": orders,
        "store_phone": store.support_phone, "paper": paper, "formats": LABEL_FORMATS,
        "base_query": query.urlencode(), "auto_print": request.GET.get("print") == "1",
        "courier_labels": [order for order in orders if order.courier_label]})


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
            if target_type == "product":
                # A product that has no stock entry yet starts one in the main warehouse.
                product = get_object_or_404(Product, id=request.POST.get("target_id"),
                    store=store, is_active=True)
                warehouse = Warehouse.objects.filter(store=store, is_active=True).order_by(
                    "priority", "id").first()
                if warehouse is None:
                    messages.error(request, "Tambahkan gudang terlebih dahulu.")
                    return redirect(back)
                level, _ = InventoryLevel.objects.select_for_update().get_or_create(
                    warehouse=warehouse, product=product)
                target_type = "level"
            else:
                level = None
            if target_type == "level":
                level = level or get_object_or_404(InventoryLevel.objects.select_for_update(),
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
    # Simple products nobody has stocked yet would otherwise be missing from this page.
    unstocked = Product.objects.filter(store=store, is_active=True, is_preorder=False, is_digital=False,
        inventory_levels__isnull=True).exclude(variants__is_active=True).order_by("name")
    if query:
        unstocked = unstocked.filter(models.Q(name__icontains=query) | models.Q(sku__icontains=query))
    unstocked = list(unstocked.distinct()[:MERCHANT_PAGE_SIZE])
    levels, level_range, level_query = paginate(request, levels, MERCHANT_PAGE_SIZE)
    variants, variant_range, variant_query = paginate(request, variants, MERCHANT_PAGE_SIZE,
        param="vpage")
    return render(request, "merchant/stock.html", _merchant_context(request, store,
        levels=levels, level_range=level_range, level_query=level_query, variants=variants,
        unstocked=unstocked,
        variant_range=variant_range, variant_query=variant_query, query=query))

@login_required(login_url="/merchant/login/")
def merchant_order_update(request, order_id):
    if request.method != "POST": raise Http404
    store = _staff_store(request)
    new_status = request.POST.get("status", "")
    if new_status in {"refund_requested", "partially_refunded", "refunded"}:
        messages.error(request, "Status refund hanya dapat diubah melalui proses refund terverifikasi.")
        return _back_to_orders(request)
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
            return _back_to_orders(request)
        try:
            transition_order(order, new_status, note=request.POST.get("note", "").strip(),
                actor=request.user)
        except InvalidOrderTransition as error:
            messages.error(request, str(error))
            return _back_to_orders(request)
        if posted_tracking is not None and tracking_number != order.tracking_number:
            order.tracking_number = tracking_number
            order.tracking_status = "ready_to_track" if tracking_number else "not_ready"
            order.save(update_fields=["tracking_number", "tracking_status"])
        AuditLog.objects.create(store=store, actor=request.user, action="order.status_updated",
            object_type="Order", object_id=str(order.id), details={"from": old_status, "to": new_status})
    messages.success(request, "Pesanan diperbarui.")
    return _back_to_orders(request)


@login_required(login_url="/merchant/login/")
def merchant_order_tracking(request, order_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    order = get_object_or_404(Order, id=order_id, store=store)
    if order.status in {"pending", "payment_failed", "cancelled", "refunded"}:
        messages.error(request, "Pengiriman belum dapat diatur untuk status pesanan ini.")
        return _back_to_orders(request)
    error = _apply_tracking(order, request.POST)
    if error:
        messages.error(request, error)
        return _back_to_orders(request)
    AuditLog.objects.create(store=store, actor=request.user, action="order.tracking_updated",
        object_type="Order", object_id=str(order.id), details={
            "tracking_number": order.tracking_number, "courier_code": order.courier_code,
            "fulfillment_method": order.fulfillment_method})
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
    return _back_to_orders(request)


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
    return _back_to_orders(request)


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
        return _back_to_orders(request)
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
    return _back_to_orders(request)

@login_required(login_url="/merchant/login/")
def merchant_order_confirm_transfer(request, order_id):
    """The seller saw the buyer's bank transfer arrive in their own account."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "finance"})
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), id=order_id, store=store,
            payment_provider="bank_transfer")
        if order.status != "pending":
            messages.error(request, "Pesanan ini tidak sedang menunggu pembayaran.")
            return _back_to_orders(request)
        confirm_payment(order, provider="bank_transfer", external_id=order.payment_reference,
            amount=order.total, currency="IDR", payload={"confirmed_by": request.user.get_username()},
            note=f"Transfer bank dikonfirmasi oleh {request.user.get_username()}")
        AuditLog.objects.create(store=store, actor=request.user, action="payment.transfer_confirmed",
            object_type="Order", object_id=str(order.id), details={"amount": str(order.total)})
    messages.success(request, "Transfer dikonfirmasi. Pesanan siap diproses.")
    return _back_to_orders(request)


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


@login_required(login_url="/merchant/login/")
def merchant_subscribers_csv(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops"})
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="newsletter-subscribers.csv"'
    writer = csv.writer(response)
    writer.writerow(["email", "source", "subscribed_at"])
    for subscriber in store.newsletter_subscribers.filter(unsubscribed_at__isnull=True).order_by("id"):
        writer.writerow([subscriber.email, subscriber.source, subscriber.subscribed_at.isoformat()])
    return response


BACKOFFICE = {
    "flash-sales": (FlashSaleProduct, FlashSaleProductForm, "Produk flash sale", ("campaign", "product", "sale_price", "stock_limit", "per_customer_limit")),
    "campaigns": (MarketingCampaign, MarketingCampaignForm, "Flash sale & popup", ("name", "headline", "starts_at", "ends_at", "is_active")),
    "slides": (StorefrontSlide, StorefrontSlideForm, "Banner carousel", ("headline", "button_label", "position", "is_active")),
    "products": (Product, ProductForm, "Produk", ("name", "sku", "price", "variant_count",
        "stock_available", "is_active")),
    "images": (ProductImage, ProductImageForm, "Foto produk", ("product", "alt_text", "position")),
    "categories": (Category, CategoryForm, "Kategori", ("name", "slug")),
    "warehouses": (Warehouse, WarehouseForm, "Gudang & outlet", ("name", "code", "is_outlet",
        "pickup_enabled", "is_active")),
    "inventory": (InventoryLevel, InventoryForm, "Persediaan", ("warehouse", "product", "on_hand", "reserved", "safety_stock", "available")),
    "shipping": (ShippingMethod, ShippingMethodForm, "Pengiriman", ("name", "provider", "flat_rate", "free_above", "is_active")),
    "coupons": (Coupon, CouponForm, "Voucher", ("code", "kind", "value", "minimum_spend", "used_count", "is_active")),
    "pages": (StorePage, StorePageForm, "Halaman brand", ("title", "slug", "show_in_header", "show_in_footer", "is_published")),
    "reviews": (ProductReview, MerchantReviewForm, "Ulasan produk", ("product", "rating", "display_name", "title", "is_published")),
    "subscribers": (NewsletterSubscriber, None, "Pelanggan newsletter", ("email", "source", "subscribed_at", "is_subscribed")),
}
# Reviews come only from verified buyers and subscribers only from their own consent, so the
# back office may moderate or export them but never create them.
NO_CREATE_SECTIONS = {"reviews", "subscribers"}
READ_ONLY_SECTIONS = {"subscribers"}


def _entity_queryset(entity, store):
    if entity in {Product, Category, Warehouse, ShippingMethod, Coupon, StorefrontSlide,
            MarketingCampaign, StorePage, NewsletterSubscriber}:
        return entity.objects.filter(store=store)
    if entity is ProductReview:
        return entity.objects.filter(store=store, product__store=store).select_related("product")
    if entity is FlashSaleProduct:
        return entity.objects.filter(product__store=store, campaign__store=store).select_related("product", "campaign")
    if entity is ProductImage:
        return entity.objects.filter(product__store=store).select_related("product")
    if entity is InventoryLevel:
        return entity.objects.filter(warehouse__store=store).select_related("warehouse", "product")
    raise Http404


@login_required(login_url="/merchant/login/")
def merchant_entity_list(request, section):
    store = _staff_store(request)
    if section == "variants":  # variants live on their product's page now
        return redirect("merchant-entity-list", section="products")
    if section not in BACKOFFICE:
        raise Http404
    entity, _, title, columns = BACKOFFICE[section]
    rows = _entity_queryset(entity, store).order_by("-id")
    if entity is Product:
        rows = annotate_available_stock(rows).annotate(variant_count=models.Subquery(
            ProductVariant.objects.filter(product=models.OuterRef("pk"), is_active=True)
            .values("product").annotate(total=Count("id")).values("total"),
            output_field=models.IntegerField()))
    query = request.GET.get("q", "").strip()
    if query:
        if entity is Product: rows = rows.filter(models.Q(name__icontains=query) |
            models.Q(sku__icontains=query) | models.Exists(ProductVariant.objects.filter(
                product=models.OuterRef("pk"), sku__icontains=query)))
        elif entity is Coupon: rows = rows.filter(code__icontains=query)
        elif entity is NewsletterSubscriber: rows = rows.filter(email__icontains=query)
        elif entity is StorePage: rows = rows.filter(title__icontains=query)
        elif entity is ProductReview: rows = rows.filter(models.Q(product__name__icontains=query) | models.Q(body__icontains=query))
        elif hasattr(entity, "name"): rows = rows.filter(name__icontains=query)
    page, page_range, page_query = paginate(request, rows, MERCHANT_PAGE_SIZE)
    table_rows = [{"object": row, "values": [_list_cell(getattr(row, column), column)
        for column in columns]} for row in page.object_list]
    return render(request, "merchant/entity_list.html", _merchant_context(request, store,
        section=section, title=title, columns=[_column_label(entity, column) for column in columns],
        table_rows=table_rows, page_obj=page,
        page_range=page_range, page_query=page_query,
        can_create=section not in NO_CREATE_SECTIONS, can_edit=section not in READ_ONLY_SECTIONS))


# Column headers of the back-office lists, in Indonesian.
COLUMN_LABELS = {"name": "Nama", "sku": "SKU", "price": "Harga", "stock_available": "Stok tersedia",
    "is_active": "Status", "code": "Kode", "is_outlet": "Outlet", "priority": "Prioritas",
    "headline": "Judul", "starts_at": "Mulai", "ends_at": "Berakhir", "button_label": "Tombol",
    "position": "Urutan", "product": "Produk", "alt_text": "Keterangan", "stock": "Stok",
    "slug": "Slug", "warehouse": "Gudang", "on_hand": "Stok fisik", "reserved": "Dipesan",
    "safety_stock": "Stok pengaman", "available": "Tersedia", "provider": "Penyedia",
    "flat_rate": "Tarif", "free_above": "Gratis di atas", "kind": "Jenis", "value": "Nilai",
    "minimum_spend": "Minimal belanja", "used_count": "Terpakai", "title": "Judul",
    "show_in_header": "Di menu atas", "show_in_footer": "Di footer", "is_published": "Tampil",
    "rating": "Rating", "display_name": "Nama pembeli", "email": "Email", "source": "Sumber",
    "subscribed_at": "Berlangganan sejak", "is_subscribed": "Aktif", "campaign": "Kampanye",
    "sale_price": "Harga promo", "stock_limit": "Kuota", "per_customer_limit": "Maks. per pembeli",
    "pickup_enabled": "Ambil di outlet", "variant_count": "Varian"}
MONEY_COLUMNS = {"price", "flat_rate", "free_above", "minimum_spend", "sale_price"}


def _column_label(entity, column):
    if column in COLUMN_LABELS:
        return COLUMN_LABELS[column]
    try:
        return str(entity._meta.get_field(column).verbose_name).capitalize()
    except FieldDoesNotExist:  # an annotation or property, e.g. "available"
        return column.replace("_", " ").capitalize()


def _list_cell(value, column):
    """Rupiah with thousand separators and readable dates, instead of 85000,00."""
    if value is None or isinstance(value, bool):
        return value
    if column in MONEY_COLUMNS:
        return "Rp" + f"{value:,.0f}".replace(",", ".")
    if hasattr(value, "strftime"):
        return timezone.localtime(value).strftime("%d %b %Y %H:%M") if hasattr(value, "hour") else (
            value.strftime("%d %b %Y"))
    return value


@login_required(login_url="/merchant/login/")
def merchant_entity_edit(request, section, object_id=None):
    store = _staff_store(request)
    if section == "variants":  # old links: open the variant's product instead
        variant = get_object_or_404(ProductVariant, id=object_id, product__store=store) if (
            object_id) else None
        if variant is None:
            return redirect("merchant-entity-list", section="products")
        return redirect(reverse("merchant-entity-edit", args=["products", variant.product_id])
            + "#variants")
    if section not in BACKOFFICE:
        raise Http404
    if section in {"inventory", "warehouses"}:
        _require_roles(request, store, {"admin", "manager", "ops", "warehouse"})
    else:
        _require_editor(request, store)
    entity, form_class, title, _ = BACKOFFICE[section]
    if section in READ_ONLY_SECTIONS or (section in NO_CREATE_SECTIONS and not object_id):
        raise Http404
    instance = get_object_or_404(_entity_queryset(entity, store), id=object_id) if object_id else None
    form = form_class(request.POST or None, request.FILES or None, instance=instance, store=store)
    # A post without the variant block (an older client or script) leaves the variants alone.
    variant_data = request.POST if "variants-TOTAL_FORMS" in request.POST else None
    variants = (ProductVariantFormSet(variant_data, instance=instance or Product(store=store),
        store=store, prefix="variants") if entity is Product else None)
    form_ok = request.method == "POST" and form.is_valid()
    if variants is not None and variants.is_bound:
        variants.product_sku = form.cleaned_data.get("sku") if form_ok else None
        form_ok = variants.is_valid() and form_ok
    if form_ok:
        try:
            with transaction.atomic():
                _, kept = _save_entity(request, store, section, entity, form, instance, variants)
        except ValidationError as error:
            if not instance:
                form.instance.pk = None
            form.add_error(None, error.messages)
        else:
            if kept:
                messages.info(request, f"{kept} varian sudah punya riwayat pesanan, jadi dinonaktifkan "
                    "dan tidak dihapus.")
            messages.success(request, f"{title} berhasil disimpan.")
            return redirect("merchant-entity-list", section=section)
    return render(request, "merchant/entity_form.html", _merchant_context(request, store,
        section=section, title=title, form=form, editing=bool(instance), variants=variants))


def _save_entity(request, store, section, entity, form, instance, variants):
    """Save a back-office form; for products, together with its variants."""
    saved = form.save(commit=False)
    if hasattr(saved, "store_id"):
        saved.store = store
    saved.save()
    form.save_m2m()
    if hasattr(form, "save_uploads"):
        form.save_uploads(saved)
    kept = variants.save_all(saved, request.user) if variants is not None and variants.is_bound else 0
    has_variants = entity is Product and saved.variants.filter(is_active=True).exists()
    if hasattr(form, "save_stock") and not has_variants:
        form.save_stock(saved, request.user)
    if entity is Product and not instance and not has_variants:
        warehouse = (form.cleaned_data.get("warehouse") or
            Warehouse.objects.filter(store=store, is_active=True).order_by("priority", "id").first())
        if warehouse:
            InventoryLevel.objects.get_or_create(warehouse=warehouse, product=saved,
                defaults={"on_hand": form.cleaned_data.get("initial_stock") or 0})
    AuditLog.objects.create(store=store, actor=request.user,
        action=f"{section}.{'updated' if instance else 'created'}", object_type=entity.__name__,
        object_id=str(saved.id), details={"label": str(saved)})
    return saved, kept


@login_required(login_url="/merchant/login/")
def merchant_settings(request):
    store = _staff_store(request)
    _require_editor(request, store)
    form = StoreSettingsForm(request.POST or None, request.FILES or None,
        instance=store, store=store)
    if request.method == "POST" and form.is_valid():
        form.save()
        form.save_uploads(store)
        AuditLog.objects.create(store=store, actor=request.user, action="store.settings_updated",
            object_type="Store", object_id=str(store.id), details={"fields": list(form.changed_data)})
        messages.success(request, "Pengaturan toko berhasil disimpan.")
        return redirect("merchant-settings")
    return render(request, "merchant/settings.html", _merchant_context(request, store,
        title="Setup cepat", form=form, editing=True, section="settings",
        storefront_templates={key: {**preset, "font_stack": Store.FONT_STACKS[
            preset["values"]["font_family"]]} for key, preset in STOREFRONT_TEMPLATES.items()}))


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
def merchant_finance(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "finance", "viewer"})
    start = parse_date(request.GET.get("start", "") or "")
    end = parse_date(request.GET.get("end", "") or "")
    lines = JournalLine.objects.filter(entry__store=store).select_related("entry", "account")
    if start:
        lines = lines.filter(entry__date__gte=start)
    if end:
        lines = lines.filter(entry__date__lte=end)
    lines = lines.order_by("-entry__date", "-entry_id", "-debit", "account__code")
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="jurnal-{store.slug}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Tanggal", "No. jurnal", "Keterangan", "Kode akun", "Nama akun", "Debit", "Kredit"])
        for line in lines.iterator():
            writer.writerow([line.entry.date.isoformat(), line.entry_id, line.entry.memo,
                line.account.code, line.account.name, line.debit, line.credit])
        return response
    accounts = account_balances(store, start=start, end=end)
    by_code = {account.code: account for account in accounts}
    totals = {"debit": sum((a.total_debit for a in accounts), Decimal(0)),
        "credit": sum((a.total_credit for a in accounts), Decimal(0))}
    summary = {"net_revenue": sum((a.balance for a in accounts if a.type == "revenue"), Decimal(0)),
        "gateway": by_code[GATEWAY].balance if GATEWAY in by_code else Decimal(0),
        "vat": by_code[VAT].balance if VAT in by_code else Decimal(0),
        "marketplace": by_code[MARKETPLACE].balance if MARKETPLACE in by_code else Decimal(0),
        "unidentified": by_code[UNIDENTIFIED].balance if UNIDENTIFIED in by_code else Decimal(0)}
    cogs = by_code[COGS].balance if COGS in by_code else Decimal(0)
    expenses = sum((a.balance for a in accounts if a.type == "expense" and a.code != COGS),
        Decimal(0))
    profit = {"revenue": summary["net_revenue"], "cogs": cogs,
        "gross": summary["net_revenue"] - cogs, "expenses": expenses,
        "net": summary["net_revenue"] - cogs - expenses,
        "revenue_lines": [a for a in accounts if a.type == "revenue" and a.balance],
        "expense_lines": [a for a in accounts if a.type == "expense" and a.code != COGS
            and a.balance]}
    missing_cost = (Product.objects.filter(store=store, is_active=True, cost_price__isnull=True)
        .exclude(variants__is_active=True).count() + ProductVariant.objects.filter(
        product__store=store, is_active=True, cost_price__isnull=True,
        product__cost_price__isnull=True).count())
    return render(request, "merchant/finance.html", _merchant_context(request, store,
        accounts=accounts, totals=totals, summary=summary, profit=profit,
        missing_cost=missing_cost, journal_lines=lines[:200], start=start, end=end))


@login_required(login_url="/merchant/login/")
def merchant_payment_settings(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin"})
    account, _ = StorePaymentSettings.objects.get_or_create(store=store)
    form = PaymentSettingsForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(store=store, actor=request.user, action="payment_settings.updated",
            object_type="StorePaymentSettings", object_id=str(account.id),
            details={"gateway": account.gateway, "bank_transfer": account.bank_transfer_enabled})
        messages.success(request, "Pengaturan pembayaran disimpan.")
        return redirect("merchant-payment-settings")
    webhook_urls = {gateway: settings.PUBLIC_BASE_URL + reverse(f"store-{gateway}-webhook",
        args=[account.webhook_key]) for gateway in ("doku", "xendit")}
    return render(request, "merchant/payments.html", _merchant_context(request, store, form=form,
        account=account, webhook_urls=webhook_urls, options=payment_options(store),
        platform_doku=platform_doku_available(), platform_fee=settings.PLATFORM_DOKU_FEE_PERCENT,
        doku_environment=doku_environment(), doku_test_url=request.session.pop("doku_test_url", "")))


@login_required(login_url="/merchant/login/")
def merchant_platform_balance(request):
    """Saldo DOKU Lakuin: what buyers paid through the platform's DOKU account for this store,
    and the payouts Lakuin made or is making to the store's bank account."""
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "finance"})
    account = StorePaymentSettings.objects.filter(store=store).first()
    if request.method == "POST":
        _require_roles(request, store, {"admin"})
        with transaction.atomic():
            Store.objects.select_for_update().get(id=store.id)  # one request at a time
            amount = platform_balance(store)["available"]
            if not account or not account.bank_account_number:
                messages.error(request, "Isi rekening bank di menu Pembayaran terlebih dahulu.")
            elif amount < MIN_PLATFORM_PAYOUT:
                messages.error(request, f"Saldo yang bisa dicairkan minimal "
                    f"Rp{MIN_PLATFORM_PAYOUT:,.0f}.".replace(",", "."))
            else:
                payout = PlatformPayout.objects.create(store=store, amount=amount,
                    bank_name=account.bank_name, bank_account_number=account.bank_account_number,
                    bank_account_holder=account.bank_account_holder, requested_by=request.user)
                AuditLog.objects.create(store=store, actor=request.user,
                    action="platform_payout.requested", object_type="PlatformPayout",
                    object_id=str(payout.id), details={"amount": str(amount)})
                messages.success(request, f"Pencairan Rp{amount:,.0f} diajukan. Dana ditransfer ke "
                    f"{account.bank_name} a.n. {account.bank_account_holder} dalam 1–2 hari kerja."
                    .replace(",", "."))
        return redirect("merchant-platform-balance")
    orders = platform_orders(store).exclude(status__in={"pending", "payment_failed"}).order_by(
        "-created_at")[:50]
    return render(request, "merchant/platform_balance.html", _merchant_context(request, store,
        account=account, summary=platform_balance(store), orders=orders,
        payouts=store.platform_payouts.all()[:20], minimum=MIN_PLATFORM_PAYOUT))


@login_required(login_url="/merchant/login/")
def merchant_payment_test(request):
    """Tes koneksi: open a Rp10.000 DOKU test page with the seller's saved keys."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin"})
    account = StorePaymentSettings.objects.filter(store=store).first()
    if not account or account.gateway != "doku" or not account.gateway_ready:
        messages.error(request, "Simpan Client ID dan Secret Key DOKU terlebih dahulu.")
        return redirect("merchant-payment-settings")
    try:
        url = DokuPaymentProvider(client_id=account.doku_client_id,
            secret_key=account.secret_key).check_credentials()
    except ProviderError as error:
        messages.error(request, f"Koneksi DOKU gagal: {error}")
    else:
        request.session["doku_test_url"] = url
        messages.success(request, f"Koneksi DOKU berhasil ({doku_environment()}). Halaman "
            "pembayaran tes Rp10.000 berhasil dibuat; tidak perlu dibayar.")
    AuditLog.objects.create(store=store, actor=request.user, action="payment_settings.tested",
        object_type="StorePaymentSettings", object_id=str(account.id), details={"gateway": "doku"})
    return redirect("merchant-payment-settings")


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
