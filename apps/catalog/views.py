from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from .models import Product


def home(request):
    if not request.store: return render(request, "storefront/no_store.html", status=503)
    products = Product.objects.filter(store=request.store, is_active=True).select_related(
        "category"
    ).prefetch_related("inventory_levels", "variants")
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    if query: products = products.filter(models.Q(name__icontains=query) | models.Q(description__icontains=query) | models.Q(sku__icontains=query))
    if category: products = products.filter(category__slug=category)
    ordering = request.GET.get("sort")
    if ordering in {"price", "-price", "name", "-created_at"}: products = products.order_by(ordering)
    return render(request, f"storefront/{request.store.theme}/home.html", {"products": products,
        "categories": request.store.categories.all(), "query": query})

def product_detail(request, slug):
    if not request.store: raise Http404
    product = get_object_or_404(Product.objects.prefetch_related("inventory_levels", "variants"),
        store=request.store, slug=slug, is_active=True)
    return render(request, "storefront/product_detail.html", {"product": product})
