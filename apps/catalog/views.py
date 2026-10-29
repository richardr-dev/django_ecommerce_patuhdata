import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, models, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from apps.orders.engagement import record_event
from apps.orders.forms import NewsletterForm, ProductReviewForm
from apps.orders.sales import active_sale_offers, decorate_products_with_sales
from apps.shops.models import NewsletterSubscriber

from .models import Product, ProductReview, WishlistItem
from .reviews import rating_summary, reviewer_display_name, verifying_order

CATALOG_PAGE_SIZE = 24
# A unique tiebreaker keeps pagination stable when many products share a price or name.
CATALOG_ORDERINGS = {"": ("-created_at", "-id"), "price": ("price", "id"),
    "-price": ("-price", "-id"), "name": ("name", "id")}


def paginate(request, queryset, per_page, *, param="page"):
    """Return (page, elided page range, query string without the page parameter)."""
    page = Paginator(queryset, per_page).get_page(request.GET.get(param))
    params = request.GET.copy()
    params.pop(param, None)
    page_range = list(page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1))
    return page, page_range, params.urlencode()


def home(request):
    if not request.store: return render(request, "storefront/no_store.html", status=503)
    products = _storefront_products(request.store)
    query = request.GET.get("q", "").strip()
    record_event(request, "search" if query else "home_view", metadata={"query": query} if query else {})
    category = request.GET.get("category", "").strip()
    if query: products = products.filter(models.Q(name__icontains=query) | models.Q(description__icontains=query) | models.Q(sku__icontains=query))
    if category: products = products.filter(category__slug=category)
    sort = request.GET.get("sort", "")
    products = products.order_by(*CATALOG_ORDERINGS.get(sort, CATALOG_ORDERINGS[""]))
    page, page_range, page_query = paginate(request, products, CATALOG_PAGE_SIZE)
    products = decorate_products_with_sales(request.store, page.object_list)
    featured_products = decorate_products_with_sales(request.store, request.store.products.filter(
        is_active=True, is_featured=True).select_related("category").prefetch_related(
        "inventory_levels", "variants").order_by("-created_at", "-id")[:8])
    now = timezone.now()
    active_campaign = request.store.marketing_campaigns.filter(
        is_active=True, starts_at__lte=now, ends_at__gt=now).first()
    campaign_products = ([offer.product for offer in
        active_campaign.product_offers.select_related("product__category").prefetch_related(
        "product__inventory_levels", "product__variants")[:24]] if active_campaign else [])
    active_offers = active_sale_offers(request.store, [product.id for product in campaign_products])
    flash_sale_products = decorate_products_with_sales(request.store, [product for product in
        campaign_products if product.id in active_offers and
        active_offers[product.id].campaign_id == active_campaign.id][:8])
    return render(request, "storefront/customizable_home.html", {"products": products,
        "page_obj": page, "page_range": page_range, "page_query": page_query,
        "product_total": page.paginator.count, "sort": sort,
        "browsing": bool(query or category or page.number > 1),
        "categories": request.store.categories.order_by("name"), "slides": list(request.store.storefront_slides.filter(is_active=True)),
        "active_campaign": active_campaign, "flash_sale_products": flash_sale_products,
        "featured_products": featured_products, "query": query})

RELATED_PRODUCT_LIMIT = 4
REVIEWS_SHOWN = 20


def _storefront_products(store):
    return Product.objects.filter(store=store, is_active=True).select_related(
        "category").prefetch_related("inventory_levels", "variants")


def _json_ld(data):
    """Serialize structured data for a <script type="application/ld+json"> block. Escaping the
    HTML-significant characters stops product text from closing the script element."""
    text = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False)
    return mark_safe(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _product_structured_data(request, product, summary):
    data = {"@context": "https://schema.org", "@type": "Product", "name": product.name,
        "sku": product.sku, "description": product.seo_description or product.description[:500],
        "brand": {"@type": "Brand", "name": request.store.name},
        "url": request.build_absolute_uri(request.path),
        "offers": {"@type": "Offer", "price": str(product.display_price),
            "priceCurrency": request.store.currency,
            "availability": "https://schema.org/" + ("InStock" if product.available_stock else "OutOfStock"),
            "url": request.build_absolute_uri(request.path)}}
    if product.primary_image_url:
        data["image"] = request.build_absolute_uri(product.primary_image_url)
    if summary["count"]:
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": summary["average"],
            "reviewCount": summary["count"]}
    return _json_ld(data)


def product_detail(request, slug):
    if not request.store: raise Http404
    product = get_object_or_404(Product.objects.prefetch_related(
        "inventory_levels", "variants", "images"),
        store=request.store, slug=slug, is_active=True)
    record_event(request, "product_view", product=product)
    product = decorate_products_with_sales(request.store, [product])[0]
    related = _storefront_products(request.store).exclude(id=product.id)
    if product.category_id:
        related = related.filter(category_id=product.category_id)
    related_products = decorate_products_with_sales(request.store,
        related.order_by("-is_featured", "-created_at", "-id")[:RELATED_PRODUCT_LIMIT])
    summary = rating_summary(product)
    reviews = product.reviews.filter(is_published=True).order_by("-created_at")[:REVIEWS_SHOWN]
    own_review = can_review = in_wishlist = None
    if request.user.is_authenticated:
        own_review = product.reviews.filter(user=request.user).first()
        can_review = bool(verifying_order(request.user, product))
        in_wishlist = WishlistItem.objects.filter(user=request.user, product=product).exists()
    return render(request, "storefront/product_detail.html", {"product": product,
        "related_products": related_products, "rating": summary, "reviews": reviews,
        "own_review": own_review, "can_review": can_review, "in_wishlist": in_wishlist,
        "review_form": ProductReviewForm(instance=own_review) if can_review else None,
        "structured_data": _product_structured_data(request, product, summary)})


@require_POST
def product_review(request, slug):
    if not request.store: raise Http404
    product = get_object_or_404(Product, store=request.store, slug=slug, is_active=True)
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={product_url(product)}")
    order = verifying_order(request.user, product)
    if not order:
        messages.error(request, "Ulasan dapat ditulis setelah pesanan produk ini diterima.")
        return redirect(product_url(product))
    review = ProductReview.objects.filter(product=product, user=request.user).first()
    form = ProductReviewForm(request.POST, instance=review)
    if form.is_valid():
        review = form.save(commit=False)
        review.store, review.product, review.user, review.order = request.store, product, request.user, order
        review.display_name = reviewer_display_name(request.user)
        try:
            with transaction.atomic():
                review.save()
        except IntegrityError:
            messages.error(request, "Ulasan Anda sudah tersimpan. Muat ulang halaman untuk mengubahnya.")
        else:
            messages.success(request, "Terima kasih, ulasan Anda sudah tersimpan.")
    else:
        messages.error(request, "Lengkapi penilaian dan isi ulasan terlebih dahulu.")
    return redirect(product_url(product) + "#ulasan")


def product_url(product):
    return reverse("product-detail", kwargs={"slug": product.slug})


def wishlist_toggle(request, product_id):
    if not request.store: raise Http404
    product = get_object_or_404(Product, store=request.store, id=product_id, is_active=True)
    if request.method != "POST":
        return redirect(product_url(product))
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={product_url(product)}")
    deleted, _ = WishlistItem.objects.filter(user=request.user, product=product).delete()
    if deleted:
        messages.success(request, f"{product.name} dihapus dari wishlist.")
    else:
        WishlistItem.objects.get_or_create(store=request.store, user=request.user, product=product)
        record_event(request, "wishlist_add", product=product)
        messages.success(request, f"{product.name} disimpan ke wishlist.")
    return redirect(_safe_back(request, product_url(product)))


def account_wishlist(request):
    if not request.store: raise Http404
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    products = _storefront_products(request.store).filter(
        wishlist_items__user=request.user).order_by("-wishlist_items__created_at")
    return render(request, "storefront/wishlist.html", {
        "products": decorate_products_with_sales(request.store, products)})


def _safe_back(request, fallback):
    target = request.POST.get("next") or request.headers.get("Referer", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        return target
    return fallback


@require_POST
def newsletter_subscribe(request):
    if not request.store: raise Http404
    form = NewsletterForm(request.POST)
    if form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        subscriber, created = NewsletterSubscriber.objects.get_or_create(store=request.store,
            email=email, defaults={"source": request.POST.get("source", "footer")[:40]})
        if not created and subscriber.unsubscribed_at:
            NewsletterSubscriber.objects.filter(id=subscriber.id).update(
                unsubscribed_at=None, subscribed_at=timezone.now())
        # The same message either way, so the form never reveals who is already subscribed.
        messages.success(request, "Terima kasih! Anda akan menerima kabar terbaru dari kami.")
    else:
        messages.error(request, next(iter(form.errors.values()))[0])
    return redirect(_safe_back(request, "/"))


def newsletter_unsubscribe(request, token):
    if not request.store: raise Http404
    subscriber = get_object_or_404(NewsletterSubscriber, store=request.store, unsubscribe_token=token)
    # Unsubscribing needs a POST so mail scanners that prefetch links cannot trigger it.
    if request.method == "POST" and subscriber.is_subscribed:
        subscriber.unsubscribed_at = timezone.now()
        subscriber.save(update_fields=["unsubscribed_at"])
    return render(request, "storefront/unsubscribe.html", {"subscriber": subscriber})


def store_page(request, slug):
    if not request.store: raise Http404
    page = get_object_or_404(request.store.pages, slug=slug, is_published=True)
    return render(request, "storefront/page.html", {"page": page})


SITEMAP_URL_LIMIT = 50000


def robots_txt(request):
    lines = ["User-agent: *"]
    if request.store:
        lines += [f"Disallow: {path}" for path in ("/merchant/", "/cart/", "/checkout/",
            "/account/", "/orders/", "/wishlist/", "/newsletter/")]
        lines.append(f"Sitemap: {request.build_absolute_uri(reverse('sitemap'))}")
    else:
        lines.append("Disallow: /")
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def sitemap_xml(request):
    if not request.store: raise Http404
    base = request.build_absolute_uri("/").rstrip("/")
    pages = request.store.pages.filter(is_published=True).values_list("slug", "updated_at")
    products = (Product.objects.filter(store=request.store, is_active=True)
        .order_by("id").values_list("slug", "created_at")[:SITEMAP_URL_LIMIT - len(pages) - 1])
    entries = [(f"{base}/", None)]
    entries += [(f"{base}{reverse('store-page', kwargs={'slug': slug})}", updated) for slug, updated in pages]
    entries += [(f"{base}{reverse('product-detail', kwargs={'slug': slug})}", created)
        for slug, created in products]
    return render(request, "storefront/sitemap.xml", {"entries": entries},
        content_type="application/xml")


def privacy_policy(request):
    if not request.store:
        raise Http404
    return render(request, "storefront/privacy.html")


def terms_of_service(request):
    if not request.store:
        raise Http404
    if request.store.terms_url:
        return redirect(request.store.terms_url)
    return render(request, "storefront/terms.html")
