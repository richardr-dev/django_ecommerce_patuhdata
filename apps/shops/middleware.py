from django.conf import settings
from django.shortcuts import redirect, render

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
