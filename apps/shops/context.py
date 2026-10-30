from django.conf import settings
from django.db.models import Q
from django.utils.functional import SimpleLazyObject

from .tenancy import platform_url


def store_context(request):
    store = getattr(request, "store", None)

    def nav_pages():
        if not store:
            return []
        return list(store.pages.filter(Q(show_in_header=True) | Q(show_in_footer=True),
            is_published=True).only("title", "slug", "show_in_header", "show_in_footer"))

    # Lazy, so responses that never render the header or footer skip the query.
    return {"store": store, "store_nav_pages": SimpleLazyObject(nav_pages)}


def platform_context(request):
    return {"platform_name": settings.PLATFORM_NAME,
        "platform_tagline": settings.PLATFORM_TAGLINE, "platform_home_url": platform_url("/"),
        "platform_company_name": settings.PLATFORM_COMPANY_NAME,
        "platform_company_url": settings.PLATFORM_COMPANY_URL,
        "platform_support_email": settings.PLATFORM_SUPPORT_EMAIL,
        "is_platform_host": getattr(request, "is_platform", False)}
