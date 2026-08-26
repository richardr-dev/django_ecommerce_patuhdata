import csv
import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalog.models import Category, InventoryLevel, Product, ProductVariant, Warehouse
from apps.integrations.http import ProviderError
from apps.integrations.models import OutboxEvent
from apps.integrations.payments import get_payment_provider
from apps.integrations.shipping import BiteshipProvider, RajaOngkirProvider
from apps.shops.models import AuditLog, Customer, Store

from .cart import get_cart
from .forms import (
    CategoryForm,
    CouponForm,
    InventoryForm,
    ProductForm,
    ShippingMethodForm,
    StoreSettingsForm,
    VariantForm,
    WarehouseForm,
)
from .models import (
    Coupon,
    InventoryAllocation,
    Order,
    OrderItem,
    OrderStatusEvent,
    PaymentTransaction,
    ReturnRequest,
    ShippingMethod,
)
from .services import (
    InvalidOrderTransition,
    allowed_order_transitions,
    calculate_discount,
    get_coupon,
    normalize_indonesian_phone,
    transition_order,
)


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
        return bool(settings.RAJAONGKIR_API_KEY and settings.RAJAONGKIR_ORIGIN_ID)
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
            if not area_id: return JsonResponse({"error": "Pilih alamat dari hasil pencarian RajaOngkir.",
                "code": "missing_destination_id"}, status=422)
            quotes = _shipping_provider().rates(area_id, lines)
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
    return JsonResponse({"quotes": response_quotes, "provider_name": _shipping_provider_name()})


def cart_add(request, product_id):
    if request.method != "POST" or not request.store: raise Http404
    product = get_object_or_404(Product, id=product_id, store=request.store, is_active=True)
    cart = request.session.get("cart", {})
    product = Product.objects.prefetch_related("inventory_levels").get(id=product.id)
    variant = None
    variant_id = request.POST.get("variant_id", "").strip()
    if product.variants.filter(is_active=True).exists():
        variant = get_object_or_404(ProductVariant, id=variant_id, product=product, is_active=True)
    key = f"v:{variant.id}" if variant else f"p:{product.id}"
    available = variant.stock if variant else product.available_stock
    try: requested = max(1, int(request.POST.get("quantity", 1)))
    except ValueError: requested = 1
    cart[key] = min(int(cart.get(key, 0)) + requested, available)
    request.session["cart"] = cart
    messages.success(request, f"{product.name} ditambahkan ke keranjang.")
    return redirect("cart")

def cart_update(request, item_key):
    if request.method != "POST" or not request.store: raise Http404
    kind, _, raw_id = item_key.partition(":")
    if not raw_id: kind, raw_id = "p", kind
    if kind == "v":
        variant = get_object_or_404(ProductVariant.objects.select_related("product"), id=raw_id,
            product__store=request.store, product__is_active=True, is_active=True)
        available = variant.stock
    else:
        product = get_object_or_404(Product.objects.prefetch_related("inventory_levels"), id=raw_id, store=request.store)
        available = product.available_stock
    try: quantity = max(0, min(int(request.POST.get("quantity", 1)), available))
    except ValueError: quantity = 1
    cart = request.session.get("cart", {})
    if quantity: cart[item_key] = quantity
    else: cart.pop(item_key, None)
    request.session["cart"] = cart
    return redirect("cart")

def cart_view(request):
    if not request.store: raise Http404
    lines, subtotal = get_cart(request)
    return render(request, "storefront/cart.html", {"lines": lines, "subtotal": subtotal})

def checkout(request):
    if not request.store: raise Http404
    lines, subtotal = get_cart(request)
    if not lines: return redirect("cart")
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
    discount, shipping_cost = calculate_discount(coupon, subtotal, shipping_cost)
    if request.method == "POST":
        required = ["customer_name", "phone", "shipping_address"]
        if any(not request.POST.get(field, "").strip() for field in required):
            messages.error(request, "Semua data pelanggan wajib diisi.")
        elif not selected_shipping and not live_quote:
            messages.error(request, "Pilih layanan pengiriman.")
        elif live_quote and live_quote["postal_code"] != request.POST.get("postal_code", "").strip():
            messages.error(request, "Kode pos berubah. Silakan cek ulang ongkir.")
        elif live_quote and live_quote["destination_area_id"] != request.POST.get("destination_area_id", "").strip():
            messages.error(request, "Alamat tujuan berubah. Silakan cek ulang ongkir.")
        elif live_quote and live_quote["cart_signature"] != _cart_signature(lines):
            messages.error(request, "Keranjang berubah. Silakan cek ulang ongkir.")
        elif settings.PAYMENT_PROVIDER == "xendit" and not settings.XENDIT_SECRET_KEY:
            messages.error(request, "Xendit belum dikonfigurasi. Tambahkan secret key pada environment.")
        else:
            try: phone = normalize_indonesian_phone(request.POST["phone"])
            except ValueError as error:
                messages.error(request, str(error))
                return render(request, "storefront/checkout.html", {"lines": lines, "subtotal": subtotal, "shipping_methods": shipping_methods})
            with transaction.atomic():
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
                    (locked_variants.get(x["variant"].id).stock if locked_variants.get(x["variant"].id) else 0) < x["quantity"]
                    if x["variant"] else sum(level.available for level in by_product[x["product"].id]) < x["quantity"]
                    for x in lines
                )
                if insufficient:
                    messages.error(request, "Stok berubah. Silakan periksa keranjang.")
                    return redirect("cart")
                tax = (subtotal * request.store.tax_rate / Decimal(100)).quantize(Decimal("0.01"))
                total = subtotal - discount + shipping_cost + tax
                order = Order.objects.create(store=request.store, subtotal=subtotal, tax=tax,
                    shipping_cost=shipping_cost, discount=discount, total=total,
                    customer_name=request.POST["customer_name"].strip(), email=request.POST.get("email", "").strip(),
                    phone=phone, shipping_address=request.POST["shipping_address"].strip(),
                    province=request.POST.get("province", "").strip(), city=request.POST.get("city", "").strip(),
                    district=request.POST.get("district", "").strip(), village=request.POST.get("village", "").strip(),
                    postal_code=request.POST.get("postal_code", "").strip(), delivery_notes=request.POST.get("delivery_notes", "").strip(),
                    destination_area_id=request.POST.get("destination_area_id", "").strip(),
                    payment_provider=settings.PAYMENT_PROVIDER, payment_method=request.POST.get("payment_method", "qris"),
                    shipping_provider=live_quote["provider"] if live_quote else selected_shipping.provider,
                    shipping_service=live_quote["name"] if live_quote else selected_shipping.name,
                    shipping_eta=live_quote["eta"] if live_quote else selected_shipping.eta,
                    coupon_code=coupon.code if coupon else "", idempotency_key=uuid.uuid4().hex)
                for line in lines:
                    variant = locked_variants.get(line["variant"].id) if line["variant"] else None
                    product = variant.product if variant else locked[line["product"].id]
                    item = OrderItem.objects.create(order=order, product=product, variant=variant,
                        name=product.name, sku=variant.sku if variant else product.sku,
                        variant_name=variant.name if variant else "", unit_price=line["unit_price"],
                        quantity=line["quantity"], line_total=line["total"])
                    if variant:
                        variant.stock -= line["quantity"]
                        variant.save(update_fields=["stock"])
                        continue
                    remaining = line["quantity"]
                    for level in by_product[product.id]:
                        allocated = min(remaining, level.available)
                        if not allocated: continue
                        level.reserved += allocated
                        level.save(update_fields=["reserved", "updated_at"])
                        InventoryAllocation.objects.create(order_item=item, inventory_level=level, quantity=allocated)
                        remaining -= allocated
                        if remaining == 0: break
                try:
                    payment = get_payment_provider(order.payment_provider).create_payment(order)
                except ProviderError as error:
                    transaction.set_rollback(True)
                    messages.error(request, f"Pembayaran belum dapat dibuat: {error}")
                    return redirect("checkout")
                order.payment_reference = payment.reference
                order.payment_checkout_url = payment.checkout_url
                if payment.expires_at:
                    from django.utils.dateparse import parse_datetime
                    order.payment_expires_at = parse_datetime(payment.expires_at)
                order.save(update_fields=["payment_reference", "payment_checkout_url", "payment_expires_at"])
                PaymentTransaction.objects.create(order=order, provider=order.payment_provider,
                    external_id=payment.reference, status="pending", amount=order.total, currency="IDR")
                request.session["cart"] = {}
                Customer.objects.update_or_create(store=request.store, phone=phone, defaults={"name": order.customer_name,
                    "email": order.email, "accepts_marketing": request.POST.get("accepts_marketing") == "on"})
                OrderStatusEvent.objects.create(order=order, status="pending", note="Pesanan dibuat")
                OutboxEvent.objects.create(store=request.store, topic="order.created", aggregate_id=str(order.id),
                    payload={"order_id": str(order.id), "phone": order.phone, "email": order.email,
                        "total": str(order.total), "payment_method": order.payment_method})
                if coupon:
                    coupon.used_count += 1
                    coupon.save(update_fields=["used_count"])
            return redirect(payment.checkout_url)
    return render(request, "storefront/checkout.html", {"lines": lines, "subtotal": subtotal,
        "shipping_methods": shipping_methods, "shipping_cost": shipping_cost, "discount": discount,
        "biteship_enabled": biteship_enabled,
        "biteship_configured": biteship_enabled,
        "shipping_provider_name": _shipping_provider_name()})

def order_detail(request, order_id):
    if not request.store: raise Http404
    order = get_object_or_404(Order.objects.prefetch_related("items"), id=order_id, store=request.store)
    return render(request, "storefront/order.html", {"order": order})

def request_return(request, order_id):
    if request.method != "POST" or not request.store: raise Http404
    order = get_object_or_404(Order, id=order_id, store=request.store)
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


@login_required(login_url="/platform-admin/login/")
def merchant_dashboard(request):
    store = _staff_store(request)
    realized = Order.objects.filter(store=store, status__in={"paid", "processing", "shipped", "completed"})
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
    low_stock = [product for product in Product.objects.filter(store=store, is_active=True).prefetch_related(
        "inventory_levels", "variants") if product.available_stock <= 5]
    recent = Order.objects.filter(store=store).order_by("-created_at")[:8]
    fulfillment = {status: store.orders.filter(status=status).count() for status in
        ("confirmed", "allocated", "picking", "packed", "shipped")}
    failed_integrations = OutboxEvent.objects.filter(store=store, status="failed").count()
    reconciliation_mismatches = PaymentTransaction.objects.filter(order__store=store,
        reconciled=False).exclude(status="pending").count()
    return render(request, "merchant/dashboard.html", _merchant_context(request, store,
        revenue=revenue, order_count=order_count, average_order=(revenue / order_count if order_count else 0),
        customer_count=store.customers.count(), pending_count=store.orders.filter(status="pending").count(),
        daily_sales=daily, top_products=top_products, low_stock=low_stock[:8], recent_orders=recent,
        fulfillment=fulfillment, failed_integrations=failed_integrations,
        reconciliation_mismatches=reconciliation_mismatches))

@login_required(login_url="/platform-admin/login/")
def merchant_orders(request):
    store = _staff_store(request)
    orders = Order.objects.filter(store=store).prefetch_related("items").order_by("-created_at")
    status = request.GET.get("status")
    if status: orders = orders.filter(status=status)
    orders = list(orders)
    for order in orders:
        order.available_statuses = allowed_order_transitions(order)
    return render(request, "merchant/orders.html", _merchant_context(request, store,
        orders=orders, statuses=Order.STATUS))

@login_required(login_url="/platform-admin/login/")
def merchant_order_update(request, order_id):
    if request.method != "POST": raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "warehouse", "customer_service"})
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), id=order_id, store=store)
        old_status, new_status = order.status, request.POST.get("status", "")
        order.tracking_number = request.POST.get("tracking_number", "").strip()
        order.save(update_fields=["tracking_number"])
        try:
            transition_order(order, new_status, note=request.POST.get("note", "").strip())
        except InvalidOrderTransition as error:
            messages.error(request, str(error))
            return redirect("merchant-orders")
        AuditLog.objects.create(store=store, actor=request.user, action="order.status_updated",
            object_type="Order", object_id=str(order.id), details={"from": old_status, "to": new_status})
    messages.success(request, "Pesanan diperbarui.")
    return redirect("merchant-orders")

@login_required(login_url="/platform-admin/login/")
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
    "products": (Product, ProductForm, "Produk", ("name", "sku", "price", "available_stock", "is_active")),
    "variants": (ProductVariant, VariantForm, "Varian", ("product", "name", "sku", "price", "stock", "is_active")),
    "categories": (Category, CategoryForm, "Kategori", ("name", "slug")),
    "warehouses": (Warehouse, WarehouseForm, "Gudang", ("name", "code", "priority", "is_active")),
    "inventory": (InventoryLevel, InventoryForm, "Persediaan", ("warehouse", "product", "on_hand", "reserved", "safety_stock", "available")),
    "shipping": (ShippingMethod, ShippingMethodForm, "Pengiriman", ("name", "provider", "flat_rate", "free_above", "is_active")),
    "coupons": (Coupon, CouponForm, "Voucher", ("code", "kind", "value", "minimum_spend", "used_count", "is_active")),
}


def _entity_queryset(entity, store):
    if entity in {Product, Category, Warehouse, ShippingMethod, Coupon}:
        return entity.objects.filter(store=store)
    if entity is ProductVariant:
        return entity.objects.filter(product__store=store).select_related("product")
    if entity is InventoryLevel:
        return entity.objects.filter(warehouse__store=store).select_related("warehouse", "product")
    raise Http404


@login_required(login_url="/platform-admin/login/")
def merchant_entity_list(request, section):
    store = _staff_store(request)
    if section not in BACKOFFICE:
        raise Http404
    entity, _, title, columns = BACKOFFICE[section]
    rows = _entity_queryset(entity, store).order_by("id")
    query = request.GET.get("q", "").strip()
    if query:
        if entity is Product or entity is ProductVariant: rows = rows.filter(models.Q(name__icontains=query) | models.Q(sku__icontains=query))
        elif entity is Coupon: rows = rows.filter(code__icontains=query)
        elif hasattr(entity, "name"): rows = rows.filter(name__icontains=query)
    table_rows = [{"object": row, "values": [getattr(row, column) for column in columns]} for row in rows]
    return render(request, "merchant/entity_list.html", _merchant_context(request, store,
        section=section, title=title, columns=columns, table_rows=table_rows))


@login_required(login_url="/platform-admin/login/")
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
        AuditLog.objects.create(store=store, actor=request.user,
            action=f"{section}.{'updated' if instance else 'created'}", object_type=entity.__name__,
            object_id=str(saved.id), details={"label": str(saved)})
        messages.success(request, f"{title} berhasil disimpan.")
        return redirect("merchant-entity-list", section=section)
    return render(request, "merchant/entity_form.html", _merchant_context(request, store,
        section=section, title=title, form=form, editing=bool(instance)))


@login_required(login_url="/platform-admin/login/")
def merchant_settings(request):
    store = _staff_store(request)
    _require_editor(request, store)
    form = StoreSettingsForm(request.POST or None, instance=store, store=store)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(store=store, actor=request.user, action="store.settings_updated",
            object_type="Store", object_id=str(store.id), details={"fields": list(form.changed_data)})
        messages.success(request, "Pengaturan toko berhasil disimpan.")
        return redirect("merchant-settings")
    return render(request, "merchant/entity_form.html", _merchant_context(request, store,
        title="Pengaturan toko", form=form, editing=True, section="settings"))


@login_required(login_url="/platform-admin/login/")
def merchant_customers(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "customer_service", "viewer"})
    customers = Customer.objects.filter(store=store).order_by("-created_at")
    query = request.GET.get("q", "").strip()
    if query:
        customers = customers.filter(models.Q(name__icontains=query) | models.Q(phone__icontains=query) |
            models.Q(email__icontains=query))
    return render(request, "merchant/customers.html", _merchant_context(request, store, customers=customers))


@login_required(login_url="/platform-admin/login/")
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


@login_required(login_url="/platform-admin/login/")
def merchant_integrations(request):
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "finance", "viewer"})
    events = OutboxEvent.objects.filter(store=store).order_by("-created_at")[:100]
    mismatches = PaymentTransaction.objects.filter(order__store=store, reconciled=False).exclude(
        status="pending").select_related("order").order_by("-updated_at")
    return render(request, "merchant/integrations.html", _merchant_context(request, store,
        integration_events=events, reconciliation_mismatches=mismatches))


@login_required(login_url="/platform-admin/login/")
def merchant_integration_retry(request, event_id):
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin", "manager", "ops", "finance"})
    event = get_object_or_404(OutboxEvent, id=event_id, store=store)
    event.status, event.next_retry_at, event.last_error = "pending", timezone.now(), ""
    event.save(update_fields=["status", "next_retry_at", "last_error"])
    AuditLog.objects.create(store=store, actor=request.user, action="integration.retry_requested",
        object_type="OutboxEvent", object_id=str(event.id), details={"topic": event.topic})
    messages.success(request, "Integrasi dijadwalkan untuk dicoba ulang.")
    return redirect("merchant-integrations")
