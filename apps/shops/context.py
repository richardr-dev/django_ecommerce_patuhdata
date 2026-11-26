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

    def cart_items():
        try:
            return sum(max(0, int(quantity)) for quantity in request.session.get("cart", {}).values())
        except (TypeError, ValueError):
            return 0

    def active_orders():
        from apps.orders.buyer import active_order_count
        return active_order_count(request)

    def missing_steps():
        from .availability import setup_steps
        return [step for step in setup_steps(store) if not step["done"]] if store else []

    def manage_store_url():
        from .auth import read_seller_cookie
        if not store or store.id not in read_seller_cookie(request):
            return ""
        # On a store address /books/ forwards to the platform, opening this store's dashboard.
        return "/books/" if settings.PLATFORM_DOMAIN else f"/books/?store={store.id}"

    def pending_payment():
        from apps.orders.buyer import payment_link
        from apps.orders.buyer import pending_payment as find
        order = find(request)
        return {"order": order, "url": payment_link(order)} if order else None

    state = getattr(request, "store_state", "open")
    preview = getattr(request, "store_preview", False)
    # Lazy, so responses that never render the header, footer or bottom bar skip the queries.
    return {"store": store, "store_state": state, "store_preview": preview,
        "store_closed": state == "closed" and not preview,
        "store_missing_steps": SimpleLazyObject(missing_steps),
        "store_nav_pages": SimpleLazyObject(nav_pages),
        "nav_cart_items": SimpleLazyObject(cart_items),
        "nav_active_orders": SimpleLazyObject(active_orders),
        "manage_store_url": SimpleLazyObject(manage_store_url),
        "pending_payment": SimpleLazyObject(pending_payment),
        "store_tracking": SimpleLazyObject(lambda: _tracking(store))}


def _tracking(store):
    from .tracking import tracking_config
    return tracking_config(store)


def asset_version():
    """Changes whenever the shared back-office CSS or script changes, so a browser never keeps an old copy.
    Production already serves hashed file names (ManifestStaticFilesStorage); this covers local runs too."""
    from pathlib import Path
    total = 0
    for name in ("css/merchant-shell.css", "js/merchant-ui.js"):
        try:
            total += int((Path(settings.BASE_DIR) / "static" / name).stat().st_mtime)
        except OSError:
            pass
    return str(total)


def platform_context(request):
    return {"asset_version": asset_version(), "storefront_enabled": settings.STOREFRONT_ENABLED, "platform_name": settings.PLATFORM_NAME,
        "platform_tagline": settings.PLATFORM_TAGLINE, "platform_home_url": platform_url("/"),
        "platform_company_name": settings.PLATFORM_COMPANY_NAME,
        "platform_company_url": settings.PLATFORM_COMPANY_URL,
        "platform_support_email": settings.PLATFORM_SUPPORT_EMAIL,
        "platform_sales_url": settings.PLATFORM_SALES_URL or (
            f"mailto:{settings.PLATFORM_SUPPORT_EMAIL}?subject=Paket%20Custom"
            if settings.PLATFORM_SUPPORT_EMAIL else ""),
        "is_platform_host": getattr(request, "is_platform", False)}
