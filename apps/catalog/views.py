from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.orders.engagement import record_event
from apps.orders.sales import active_sale_offers, decorate_products_with_sales

from .models import Product


def home(request):
    if not request.store: return render(request, "storefront/no_store.html", status=503)
    products = Product.objects.filter(store=request.store, is_active=True).select_related(
        "category"
    ).prefetch_related("inventory_levels", "variants")
    query = request.GET.get("q", "").strip()
    record_event(request, "search" if query else "home_view", metadata={"query": query} if query else {})
    category = request.GET.get("category", "").strip()
    if query: products = products.filter(models.Q(name__icontains=query) | models.Q(description__icontains=query) | models.Q(sku__icontains=query))
    if category: products = products.filter(category__slug=category)
    ordering = request.GET.get("sort")
    if ordering in {"price", "-price", "name", "-created_at"}: products = products.order_by(ordering)
    products = decorate_products_with_sales(request.store, products)
    featured_products = decorate_products_with_sales(request.store, request.store.products.filter(
        is_active=True, is_featured=True).prefetch_related("inventory_levels", "variants")[:8])
    now = timezone.now()
    active_campaign = request.store.marketing_campaigns.filter(
        is_active=True, starts_at__lte=now, ends_at__gt=now).first()
    campaign_products = ([offer.product for offer in
        active_campaign.product_offers.select_related("product")[:24]] if active_campaign else [])
    active_offers = active_sale_offers(request.store, [product.id for product in campaign_products])
    flash_sale_products = decorate_products_with_sales(request.store, [product for product in
        campaign_products if product.id in active_offers and
        active_offers[product.id].campaign_id == active_campaign.id][:8])
    return render(request, "storefront/customizable_home.html", {"products": products,
        "categories": request.store.categories.all(), "slides": list(request.store.storefront_slides.filter(is_active=True)),
        "active_campaign": active_campaign, "flash_sale_products": flash_sale_products,
        "featured_products": featured_products, "query": query})

def product_detail(request, slug):
    if not request.store: raise Http404
    product = get_object_or_404(Product.objects.prefetch_related(
        "inventory_levels", "variants", "images"),
        store=request.store, slug=slug, is_active=True)
    record_event(request, "product_view", product=product)
    product = decorate_products_with_sales(request.store, [product])[0]
    return render(request, "storefront/product_detail.html", {"product": product})


def privacy_policy(request):
    if not request.store:
        raise Http404
    return render(request, "storefront/privacy.html")
