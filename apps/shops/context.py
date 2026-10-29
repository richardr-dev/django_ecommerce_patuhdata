from django.db.models import Q
from django.utils.functional import SimpleLazyObject


def store_context(request):
    store = getattr(request, "store", None)

    def nav_pages():
        if not store:
            return []
        return list(store.pages.filter(Q(show_in_header=True) | Q(show_in_footer=True),
            is_published=True).only("title", "slug", "show_in_header", "show_in_footer"))

    # Lazy, so responses that never render the header or footer skip the query.
    return {"store": store, "store_nav_pages": SimpleLazyObject(nav_pages)}
