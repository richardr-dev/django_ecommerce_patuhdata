import hashlib
import json
import secrets
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.catalog.models import Product

from .loyalty import loyalty_account
from .models import MobileAccessToken, Order
from .sales import active_sale_offers, offer_remaining


def _error(message, status=400, code="invalid_request"):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _body(request):
    if request.content_type != "application/json":
        raise ValueError("Content-Type must be application/json.")
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as error:
        raise ValueError("Request body must contain valid JSON.") from error
    if not isinstance(data, dict):
        raise TypeError("Request body must be a JSON object.")
    return data


def _image(request, field):
    if not field:
        return ""
    try:
        return request.build_absolute_uri(field.url)
    except (ValueError, AttributeError):
        return str(field)


def _token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def mobile_auth(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.store:
            return _error("Store not found.", 404, "store_not_found")
        authorization = request.headers.get("Authorization", "")
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            return _error("A bearer token is required.", 401, "authentication_required")
        token = (
            MobileAccessToken.objects.select_related("user")
            .filter(
                store=request.store,
                token_hash=_token_hash(raw),
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if not token or not token.user.is_active:
            return _error("The access token is invalid or expired.", 401, "invalid_token")
        MobileAccessToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        request.mobile_user, request.mobile_token = token.user, token
        return view(request, *args, **kwargs)

    return wrapped


def _product(request, product, offer=None):
    variants = [
        {
            "id": variant.id,
            "name": variant.name,
            "sku": variant.sku,
            "attributes": variant.attributes,
            "price": str(
                min(offer.sale_price, variant.effective_price) if offer else variant.effective_price
            ),
            "regular_price": str(variant.effective_price),
            "is_flash_sale": bool(offer and offer.sale_price < variant.effective_price),
            "available_stock": variant.available_stock,
        }
        for variant in product.variants.all()
        if variant.is_active
    ]
    images = [
        {
            "url": _image(request, image.file) if image.file else image.url,
            "alt_text": image.alt_text,
        }
        for image in product.images.all()
    ]
    regular_price = product.price
    sale_price = offer.sale_price if offer and offer.sale_price < regular_price else None
    return {
        "id": product.id,
        "slug": product.slug,
        "name": product.name,
        "sku": product.sku,
        "description": product.description,
        "category": (
            {"slug": product.category.slug, "name": product.category.name}
            if product.category
            else None
        ),
        "price": str(sale_price or regular_price),
        "regular_price": str(regular_price),
        "compare_at_price": str(product.compare_at_price) if product.compare_at_price else None,
        "is_flash_sale": bool(sale_price),
        "flash_sale_ends_at": (offer.campaign.ends_at.isoformat() if sale_price else None),
        "flash_sale_remaining": offer_remaining(offer) if sale_price else None,
        "available_stock": product.available_stock,
        "image_url": (_image(request, product.photo) if product.photo else product.image_url),
        "images": images,
        "variants": variants,
    }


@require_GET
def store_detail(request):
    if not request.store:
        return _error("Store not found. Send X-Store-Slug.", 404, "store_not_found")
    store = request.store
    now = timezone.now()
    campaign = store.marketing_campaigns.filter(
        is_active=True, starts_at__lte=now, ends_at__gt=now
    ).first()
    slides = [
        {
            "id": slide.id,
            "image_url": _image(request, slide.image),
            "mobile_image_url": _image(request, slide.mobile_image),
            "eyebrow": slide.eyebrow,
            "headline": slide.headline,
            "subheadline": slide.subheadline,
            "button_label": slide.button_label,
            "link_url": slide.link_url,
        }
        for slide in store.storefront_slides.filter(is_active=True)
    ]
    return JsonResponse(
        {
            "data": {
                "slug": store.slug,
                "name": store.name,
                "description": store.description,
                "logo_url": _image(request, store.logo),
                "currency": store.currency,
                "primary_color": store.primary_color,
                "announcement": {
                    "text": store.announcement_text,
                    "link_label": store.announcement_link_label,
                    "link_url": store.announcement_link_url,
                },
                "hero": {
                    "eyebrow": store.hero_eyebrow,
                    "title": store.hero_title,
                    "text": store.hero_text,
                    "button_label": store.hero_button_label,
                },
                "loyalty_enabled": store.loyalty_enabled,
                "slides": slides,
                "campaign": (
                    {
                        "id": campaign.id,
                        "headline": campaign.headline,
                        "message": campaign.message,
                        "coupon_code": campaign.coupon_code,
                        "ends_at": campaign.ends_at.isoformat(),
                    }
                    if campaign
                    else None
                ),
            }
        }
    )


@require_GET
def products(request):
    if not request.store:
        return _error("Store not found. Send X-Store-Slug.", 404, "store_not_found")
    try:
        page = max(1, int(request.GET.get("page", 1)))
        page_size = min(100, max(1, int(request.GET.get("page_size", 20))))
    except ValueError:
        return _error("page and page_size must be integers.")
    queryset = (
        Product.objects.filter(store=request.store, is_active=True)
        .select_related("category")
        .prefetch_related("variants", "inventory_levels", "images")
    )
    query, category = request.GET.get("q", "").strip(), request.GET.get("category", "").strip()
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) | Q(description__icontains=query) | Q(sku__icontains=query)
        )
    if category:
        queryset = queryset.filter(category__slug=category)
    total = queryset.count()
    rows = list(
        queryset.order_by("-is_featured", "-created_at")[(page - 1) * page_size : page * page_size]
    )
    offers = active_sale_offers(request.store, [row.id for row in rows])
    return JsonResponse(
        {
            "data": [_product(request, row, offers.get(row.id)) for row in rows],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": page * page_size < total,
            },
        }
    )


@require_GET
def product_detail(request, slug):
    if not request.store:
        return _error("Store not found. Send X-Store-Slug.", 404, "store_not_found")
    product = (
        Product.objects.filter(store=request.store, slug=slug, is_active=True)
        .select_related("category")
        .prefetch_related("variants", "inventory_levels", "images")
        .first()
    )
    if not product:
        return _error("Product not found.", 404, "not_found")
    return JsonResponse(
        {
            "data": _product(
                request, product, active_sale_offers(request.store, [product.id]).get(product.id)
            )
        }
    )


@csrf_exempt
@require_POST
def create_token(request):
    if not request.store:
        return _error("Store not found. Send X-Store-Slug.", 404, "store_not_found")
    remote = request.META.get("REMOTE_ADDR", "unknown")
    throttle_key = f"mobile-login:{request.store_id if hasattr(request, 'store_id') else request.store.id}:{remote}"
    attempts = cache.get(throttle_key, 0)
    if attempts >= 10:
        return _error("Too many login attempts. Try again later.", 429, "rate_limited")
    try:
        data = _body(request)
    except (TypeError, ValueError) as error:
        return _error(str(error))
    email, password = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
    user = authenticate(request, username=email, password=password)
    if not user:
        cache.set(throttle_key, attempts + 1, timeout=900)
        return _error("Email or password is incorrect.", 401, "invalid_credentials")
    cache.delete(throttle_key)
    raw = secrets.token_urlsafe(32)
    expires = timezone.now() + timedelta(days=settings.MOBILE_TOKEN_TTL_DAYS)
    MobileAccessToken.objects.create(
        store=request.store, user=user, token_hash=_token_hash(raw), expires_at=expires
    )
    return JsonResponse(
        {"data": {"access_token": raw, "token_type": "Bearer", "expires_at": expires.isoformat()}},
        status=201,
    )


@csrf_exempt
@require_POST
@mobile_auth
def revoke_token(request):
    request.mobile_token.revoked_at = timezone.now()
    request.mobile_token.save(update_fields=["revoked_at"])
    return JsonResponse({"data": {"revoked": True}})


@require_GET
@mobile_auth
def me(request):
    user = request.mobile_user
    return JsonResponse(
        {
            "data": {
                "id": user.id,
                "email": user.email,
                "name": user.get_full_name(),
                "store": request.store.slug,
            }
        }
    )


@require_GET
@mobile_auth
def account_orders(request):
    orders = (
        Order.objects.filter(store=request.store, customer_user=request.mobile_user)
        .prefetch_related("items")
        .order_by("-created_at")[:100]
    )
    data = [
        {
            "id": str(order.id),
            "status": order.status,
            "status_label": order.get_status_display(),
            "subtotal": str(order.subtotal),
            "discount": str(order.discount),
            "shipping": str(order.shipping_cost),
            "total": str(order.total),
            "created_at": order.created_at.isoformat(),
            "tracking_number": order.tracking_number,
            "tracking_status": order.tracking_status,
            "items": [
                {
                    "name": item.name,
                    "variant": item.variant_name,
                    "sku": item.sku,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                    "line_total": str(item.line_total),
                }
                for item in order.items.all()
            ],
        }
        for order in orders
    ]
    return JsonResponse({"data": data})


@require_GET
@mobile_auth
def loyalty_wallet(request):
    if not request.store.loyalty_enabled:
        return _error("Loyalty is not enabled for this store.", 404, "not_enabled")
    account = loyalty_account(request.store, request.mobile_user)
    entries = [
        {
            "kind": entry.kind,
            "points": entry.points,
            "balance_after": entry.balance_after,
            "order_id": str(entry.order_id) if entry.order_id else None,
            "note": entry.note,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in account.transactions.all()[:100]
    ]
    return JsonResponse(
        {
            "data": {
                "balance": account.balance,
                "lifetime_earned": account.lifetime_earned,
                "point_value": request.store.loyalty_point_value,
                "minimum_redemption": request.store.loyalty_min_redeem,
                "transactions": entries,
            }
        }
    )
