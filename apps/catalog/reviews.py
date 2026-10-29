from django.db.models import Avg, Count

from .models import ProductReview

# Only customers who actually received the product may rate it.
REVIEWABLE_STATUSES = {"delivered", "completed"}


def verifying_order(user, product):
    """Return the most recent received order containing the product, or None."""
    if not user.is_authenticated:
        return None
    from apps.orders.models import Order
    return (Order.objects.filter(store_id=product.store_id, customer_user=user,
        status__in=REVIEWABLE_STATUSES, items__product=product).order_by("-created_at").first())


def rating_summary(product):
    published = ProductReview.objects.filter(product=product, is_published=True)
    totals = published.aggregate(average=Avg("rating"), count=Count("id"))
    counts = dict(published.values_list("rating").annotate(total=Count("id")))
    count = totals["count"] or 0
    distribution = [{"stars": stars, "count": counts.get(stars, 0),
        "percent": round(counts.get(stars, 0) * 100 / count) if count else 0} for stars in range(5, 0, -1)]
    return {"average": round(totals["average"] or 0, 1), "count": count, "distribution": distribution}


def reviewer_display_name(user):
    name = (user.get_full_name() or user.username).strip()
    first, _, rest = name.partition(" ")
    # Show "Ayu S." rather than a full name, which keeps reviews personal but not identifying.
    return f"{first} {rest[:1]}.".strip() if rest else first
