"""Product feed for Google Merchant Center (Shopping) and Meta Commerce Manager (Facebook &
Instagram Shop). Both read this RSS 2.0 feed with the g: namespace; each variant is its own
item grouped under its product."""
from django.http import Http404
from django.shortcuts import render
from django.utils.html import strip_tags

from .models import Product

FEED_LIMIT = 5000


def _absolute(request, url):
    if not url:
        return ""
    return url if url.startswith(("http://", "https://")) else request.build_absolute_uri(url)


def _price(value):
    return f"{value:.2f} IDR"


def feed_items(request, store):
    products = (Product.objects.filter(store=store, is_active=True)
        .select_related("category").prefetch_related("variants", "inventory_levels")
        .order_by("id")[:FEED_LIMIT])
    for product in products:
        link = request.build_absolute_uri(f"/products/{product.slug}/")
        base = {"title": product.name[:150], "brand": store.name[:70],
            "description": (strip_tags(product.description) or product.name)[:5000],
            "link": link, "image": _absolute(request, product.primary_image_url),
            "category": product.category.name if product.category else "",
            "preorder": product.is_preorder}
        variants = [variant for variant in product.variants.all() if variant.is_active]
        rows = ([(variant.sku, f"{product.name} {variant.name}", variant.effective_price,
            variant.available_stock, product.sku) for variant in variants] if variants
            else [(product.sku, product.name, product.price, product.available_stock, "")])
        for sku, title, price, stock, group in rows:
            regular = product.compare_at_price if product.has_discount and not variants else None
            yield {**base, "id": sku, "title": title[:150], "group": group,
                "price": _price(regular or price), "sale_price": _price(price) if regular else "",
                "availability": "preorder" if product.is_preorder else (
                    "in_stock" if stock > 0 else "out_of_stock")}


def products_feed(request):
    if not request.store:
        raise Http404
    return render(request, "storefront/products_feed.xml", {"items": feed_items(request, request.store),
        "store": request.store, "home": request.build_absolute_uri("/")},
        content_type="application/rss+xml; charset=utf-8")
