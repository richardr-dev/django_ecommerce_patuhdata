from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from apps.catalog.models import FlashSaleProduct

INACTIVE_ORDER_STATUSES = {"payment_failed", "cancelled", "refunded", "returned"}


def active_sale_offers(store, product_ids, *, lock=False):
    queryset = FlashSaleProduct.objects.filter(product_id__in=product_ids,
        product__store=store, campaign__store=store, campaign__is_active=True,
        campaign__starts_at__lte=timezone.now(), campaign__ends_at__gt=timezone.now(),
        sale_price__lt=F("product__price")).select_related(
        "campaign", "product").order_by("campaign__position", "sale_price", "id")
    if lock:
        queryset = queryset.select_for_update()
    offers = {}
    for offer in queryset:
        offers.setdefault(offer.product_id, offer)
    return offers


def offer_sold_quantity(offer, *, phone=None):
    items = offer.order_items.exclude(order__status__in=INACTIVE_ORDER_STATUSES)
    if phone:
        items = items.filter(order__phone=phone)
    return items.aggregate(total=Sum("quantity"))["total"] or 0


def offer_remaining(offer):
    if offer.stock_limit is None:
        return None
    return max(0, offer.stock_limit - offer_sold_quantity(offer))


def decorate_products_with_sales(store, products):
    products = list(products)
    offers = active_sale_offers(store, [product.id for product in products])
    for product in products:
        offer = offers.get(product.id)
        product.active_sale_offer = offer
        product.display_price = offer.sale_price if offer else product.price
        regular_compare = product.compare_at_price if product.has_discount else None
        product.display_compare_at = product.price if offer and offer.sale_price < product.price else regular_compare
        product.display_has_discount = bool(product.display_compare_at and product.display_compare_at > product.display_price)
        for variant in product.variants.all():
            regular_price = variant.effective_price
            variant.display_price = min(offer.sale_price, regular_price) if offer else regular_price
        product.display_discount_percent = (round(
            (product.display_compare_at - product.display_price) / product.display_compare_at * Decimal(100)
        ) if product.display_has_discount else 0)
    return products
