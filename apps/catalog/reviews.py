from django.db import transaction
from django.db.models import Avg, Count, Q

from .models import ProductReview, ReviewPhoto

# Only customers who actually received the product may rate it.
REVIEWABLE_STATUSES = {"delivered", "completed"}
MAX_REVIEW_PHOTOS = 3
REVIEW_SORTS = {"": ("-created_at", "-id"), "tertinggi": ("-rating", "-created_at"),
    "terendah": ("rating", "-created_at")}
# First bytes of the image formats buyers may upload; the browser's content type can be forged.
IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


def verifying_order(user, product):
    """Return the most recent received order containing the product, or None."""
    if not user.is_authenticated:
        return None
    from apps.orders.models import Order
    return (Order.objects.filter(store_id=product.store_id, customer_user=user,
        status__in=REVIEWABLE_STATUSES, items__product=product).order_by("-created_at").first())


def rating_summary(product):
    published = ProductReview.objects.filter(product=product, is_published=True)
    totals = published.aggregate(average=Avg("rating"), count=Count("id"),
        with_photos=Count("id", filter=Q(photos__isnull=False), distinct=True))
    counts = dict(published.values_list("rating").annotate(total=Count("id")))
    count = totals["count"] or 0
    distribution = [{"stars": stars, "count": counts.get(stars, 0),
        "percent": round(counts.get(stars, 0) * 100 / count) if count else 0} for stars in range(5, 0, -1)]
    return {"average": round(totals["average"] or 0, 1), "count": count,
        "with_photos": totals["with_photos"], "distribution": distribution}


def filtered_reviews(product, *, stars="", photos=False, sort=""):
    """Published reviews for the product page, filtered like a marketplace's review tabs."""
    reviews = product.reviews.filter(is_published=True).prefetch_related("photos")
    if stars in {"1", "2", "3", "4", "5"}:
        reviews = reviews.filter(rating=int(stars))
    if photos:
        reviews = reviews.filter(photos__isnull=False).distinct()
    return reviews.order_by(*REVIEW_SORTS.get(sort, REVIEW_SORTS[""]))


def short_name(name):
    # Show "Ayu S." rather than a full name, which keeps reviews personal but not identifying.
    first, _, rest = (name or "Pembeli").strip().partition(" ")
    return f"{first} {rest[:1]}.".strip() if rest else first


def reviewer_display_name(user):
    return short_name(user.get_full_name() or user.username)


def is_image(upload):
    head = upload.read(12)
    upload.seek(0)
    return head.startswith(IMAGE_SIGNATURES) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def order_review_items(order):
    """One entry per product of a received order, with the review already left for it."""
    if order.status not in REVIEWABLE_STATUSES:
        return []
    reviews = {review.product_id: review for review in ProductReview.objects.filter(
        order=order).prefetch_related("photos")}
    items, seen = [], set()
    for item in order.items.select_related("product"):
        if item.product_id in seen or not item.product.is_active:
            continue
        seen.add(item.product_id)
        items.append({"item": item, "product": item.product, "review": reviews.get(item.product_id)})
    return items


@transaction.atomic
def save_order_review(order, item, *, rating, title, body, photos, user=None):
    """Create or update the review for one product of a received order. New photos replace the
    old ones; leaving the photo field empty keeps them."""
    review = ProductReview.objects.select_for_update().filter(product=item.product, order=order).first()
    if review is None and user is not None and user.is_authenticated:
        review = ProductReview.objects.select_for_update().filter(product=item.product,
            user=user).first()  # one review per buyer and product, whichever order it came from
    review = review or ProductReview(store=order.store, product=item.product, order=order)
    review.user = review.user or (order.customer_user if order.customer_user_id else None)
    review.rating, review.title, review.body = rating, title, body
    review.variant_name = item.variant_name
    review.display_name = short_name(order.customer_name)
    review.save()
    if photos:
        for old in review.photos.all():
            old.file.delete(save=False)
            old.delete()
        for position, upload in enumerate(photos[:MAX_REVIEW_PHOTOS]):
            ReviewPhoto.objects.create(review=review, file=upload, position=position)
    return review
