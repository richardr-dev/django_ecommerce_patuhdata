"""Trust signals a marketplace would give a shopper, earned by the store itself: its rating from
verified buyers, orders delivered, how long it has sold here, and Lakuin's identity check."""
from django.core.cache import cache
from django.db.models import Avg, Count, Sum

# Orders that count as a sale once paid; cancelled and refunded ones do not.
SOLD_STATUSES = {"paid", "confirmed", "allocated", "picking", "packed", "shipped", "delivered",
    "completed"}
DELIVERED_STATUSES = {"delivered", "completed"}
CACHE_SECONDS = 600


def store_trust(store):
    from apps.catalog.models import ProductReview
    from apps.orders.models import Order

    key = f"store-trust:{store.id}"
    data = cache.get(key)
    if data is None:
        reviews = ProductReview.objects.filter(store=store, is_published=True).aggregate(
            average=Avg("rating"), count=Count("id"))
        data = {"rating": round(reviews["average"], 1) if reviews["average"] else None,
            "reviews": reviews["count"],
            "delivered": Order.objects.filter(store=store, status__in=DELIVERED_STATUSES).count()}
        cache.set(key, data, CACHE_SECONDS)
    return {**data, "since": store.created_at, "verified": bool(store.verified_at)}


def sold_label(quantity):
    """Marketplace-style: 7, 950, 1rb+, 12rb+."""
    return f"{quantity // 1000}rb+" if quantity >= 1000 else str(quantity)


def decorate_with_sales_count(store, products):
    """Give each product its units sold ("1rb+ terjual"), in one query for the whole page.
    Ratings stay on the product page and are not shown in the catalog."""
    from apps.orders.models import OrderItem

    ids = {product.id for product in products}
    if not ids:
        return products
    sold = dict(OrderItem.objects.filter(order__store=store, product__in=ids,
        order__status__in=SOLD_STATUSES).values("product").annotate(units=Sum("quantity"))
        .values_list("product", "units"))
    for product in products:
        product.sold = sold.get(product.id) or 0
        product.sold_label = sold_label(product.sold)
    return products
