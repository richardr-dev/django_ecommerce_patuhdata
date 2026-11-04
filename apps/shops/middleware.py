from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render

from .availability import (
    ALWAYS_OPEN,
    ORDERING,
    PREVIEW_SESSION_KEY,
    is_previewing,
    read_preview_token,
    store_state,
)
from .models import Store
from .tenancy import custom_domain_allowed, platform_host, platform_url, store_url, subdomain_slug


class StoreResolverMiddleware:
    """Resolve the tenant for this request.

    Platform mode (PLATFORM_DOMAIN set): the apex/www host is the platform itself (no store,
    except mobile clients naming one with X-Store-Slug), <slug>.PLATFORM_DOMAIN is that store,
    and any other host must be a store's custom domain. Hosts never fall back to another
    store, so one seller's storefront cannot be shown under another's address.

    Single-host mode: custom domain, then X-Store-Slug or ?store=slug (remembered in the
    session), then the first store on localhost."""
    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        request.is_platform = platform_host(host)
        if settings.PLATFORM_DOMAIN:
            if host == f"www.{settings.PLATFORM_DOMAIN}":
                # One canonical address for the platform (search engines, shared links).
                return redirect(platform_url(request.get_full_path()), permanent=True)
            request.store = self._platform_store(request, host)
            store = request.store
            if not store and not request.is_platform:
                # Custom domains need ALLOWED_HOSTS=*, so this is the host check: an unknown
                # Host header must never reach views that build absolute links from it.
                return render(request, "platform/store_not_found.html", status=404)
            back_office = request.path.startswith("/merchant/") or request.path.rstrip("/") == "/dashboard"
            if (store and back_office and not request.is_platform
                    and request.method in {"GET", "HEAD"}):
                # The back office lives on the platform host, where the merchant session is;
                # opening it from a store address lands on that store's dashboard.
                query = request.GET.copy()
                query["store"] = store.id
                path = request.path if request.path.startswith("/merchant/") else "/merchant/"
                return redirect(platform_url(f"{path}?{query.urlencode()}"))
            if (store and store.domain == host and request.method in {"GET", "HEAD"}
                    and not custom_domain_allowed(store)):
                # The plan lapsed: keep visitors on the store via its platform subdomain.
                return redirect(store_url(store, request.get_full_path(), use_domain=False))
            return self.get_response(request)
        request.store = self._single_host_store(request, host)
        return self.get_response(request)

    @staticmethod
    def _platform_store(request, host):
        active = Store.objects.filter(is_active=True).select_related("subscription__plan")
        if request.is_platform:
            slug = request.headers.get("X-Store-Slug")
            return active.filter(slug=slug).first() if slug else None
        slug = subdomain_slug(host)
        if slug:
            return active.filter(slug=slug).first()
        return active.filter(domain=host).first()

    @staticmethod
    def _single_host_store(request, host):
        slug = request.headers.get("X-Store-Slug") or request.GET.get("store") or request.session.get("store_slug")
        store = Store.objects.filter(domain=host, is_active=True).first()
        if not store and slug:
            store = Store.objects.filter(slug=slug, is_active=True).first()
        if not store and not slug and host in {"localhost", "127.0.0.1", "testserver"}:
            store = Store.objects.filter(is_active=True).first()
        if store: request.session["store_slug"] = store.slug
        return store


class StorefrontGateMiddleware:
    """Shoppers only see a store that is ready, and a store whose subscription lapsed says it is
    temporarily closed instead of taking orders (see apps/shops/availability.py)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        store = getattr(request, "store", None)
        request.store_state, request.store_preview = "open", False
        if (not settings.STOREFRONT_GATE or store is None or getattr(request, "is_platform", False)
                or request.path.startswith(ALWAYS_OPEN)):
            return self.get_response(request)
        token = request.GET.get("preview")
        if token and read_preview_token(token) == store.id:
            previews = request.session.get(PREVIEW_SESSION_KEY, [])
            request.session[PREVIEW_SESSION_KEY] = [*previews, store.id][-10:]
            query = request.GET.copy()
            query.pop("preview")
            return redirect(f"{request.path}?{query.urlencode()}" if query else request.path)
        request.store_state = store_state(store)
        if request.store_state == "open":
            return self.get_response(request)
        request.store_preview = is_previewing(request, store)
        if request.store_preview:
            return self.get_response(request)
        if request.store_state == "setup":
            response = render(request, "storefront/coming_soon.html", {"store": store}, status=503)
            response["Retry-After"] = "86400"
            return response
        if request.path.startswith(ORDERING):
            messages.info(request, f"{store.name} sedang tutup sementara dan belum menerima "
                "pesanan baru.")
            return redirect("/")
        return self.get_response(request)


class SellerShortcutMiddleware:
    """Remember, in a signed cookie shared with store subdomains, which stores the signed-in
    seller manages, so their own storefront can show a "Kelola toko" link. The cookie only
    lists store ids: it signs no one in, and the back office still asks for the session."""
    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.PLATFORM_DOMAIN and not getattr(request, "is_platform", False):
            return response  # set and cleared only where the seller signs in and out
        from django.core import signing

        from .auth import (
            SELLER_COOKIE,
            SELLER_COOKIE_AGE,
            SELLER_COOKIE_SALT,
            read_seller_cookie,
            seller_store_ids,
        )
        domain = f".{settings.PLATFORM_DOMAIN}" if settings.PLATFORM_DOMAIN else None
        user = getattr(request, "user", None)
        has_cookie = SELLER_COOKIE in request.COOKIES
        if user is not None and user.is_authenticated:
            if not request.path.startswith("/merchant/") and has_cookie:
                return response
            ids = seller_store_ids(user)
            if ids and set(ids) != read_seller_cookie(request):
                response.set_cookie(SELLER_COOKIE, signing.dumps(ids, salt=SELLER_COOKIE_SALT),
                    max_age=SELLER_COOKIE_AGE, domain=domain, secure=not settings.DEBUG,
                    httponly=True, samesite="Lax")
            elif not ids and has_cookie:
                response.delete_cookie(SELLER_COOKIE, domain=domain)
        elif has_cookie:
            response.delete_cookie(SELLER_COOKIE, domain=domain)
        return response


PRIVATE_PREFIXES = ("/merchant/", "/account/", "/platform-admin/", "/cart/", "/checkout/",
    "/dashboard")


class PrivatePagesMiddleware:
    """Back office, account, cart and checkout pages carry personal data: the browser must
    not keep them, so after Keluar the Back button cannot show them again."""
    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(PRIVATE_PREFIXES) and not response.has_header("Cache-Control"):
            from django.utils.cache import add_never_cache_headers
            add_never_cache_headers(response)
        return response
