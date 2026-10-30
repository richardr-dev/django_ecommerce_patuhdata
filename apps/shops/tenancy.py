"""Host rules for the self-serve platform: which host is the platform, which is a store."""
from urllib.parse import urlsplit

from django.conf import settings

# Subdomains a seller may not claim: platform infrastructure, or names that look official.
RESERVED_SLUGS = frozenset({
    "admin", "api", "app", "assets", "billing", "blog", "cdn", "dashboard", "dev", "docs",
    "ftp", "halo", "help", "internal", "lakuin", "login", "mail", "media", "merchant", "ns1", "ns2", "platform",
    "platform-admin", "shop", "signup", "smtp", "staging", "static", "status", "store", "support",
    "test", "toko", "www",
})


def _base():
    parts = urlsplit(settings.PLATFORM_BASE_URL or settings.PUBLIC_BASE_URL)
    port = f":{parts.port}" if parts.port else ""
    return parts.scheme or "https", port


def platform_host(host):
    """True for the platform's own hosts: apex, www, and PUBLIC_BASE_URL's host, where payment
    webhooks arrive (it may be a tunnel such as ngrok during development)."""
    domain = settings.PLATFORM_DOMAIN
    if not domain:
        return False
    return host in {domain, f"www.{domain}", urlsplit(settings.PUBLIC_BASE_URL).hostname}


def subdomain_slug(host):
    """The store slug for <slug>.PLATFORM_DOMAIN, or None for any other host."""
    domain = settings.PLATFORM_DOMAIN
    if not domain or not host.endswith(f".{domain}"):
        return None
    slug = host[:-len(domain) - 1]
    return slug if slug and "." not in slug and slug != "www" else None


def platform_url(path="/"):
    if not settings.PLATFORM_DOMAIN:
        return settings.PUBLIC_BASE_URL + path
    scheme, port = _base()
    return f"{scheme}://{settings.PLATFORM_DOMAIN}{port}{path}"


def store_origin(store, *, use_domain=True):
    """Scheme and host serving this store: custom domain when entitled, else the platform
    subdomain, else (single-host mode) PUBLIC_BASE_URL."""
    if use_domain and store.domain and custom_domain_allowed(store):
        return f"https://{store.domain}"
    if settings.PLATFORM_DOMAIN:
        scheme, port = _base()
        return f"{scheme}://{store.slug}.{settings.PLATFORM_DOMAIN}{port}"
    return settings.PUBLIC_BASE_URL


def store_url(store, path="/", *, use_domain=True):
    """Absolute link a visitor can open; single-host mode selects the store by ?store=."""
    url = store_origin(store, use_domain=use_domain) + path
    if settings.PLATFORM_DOMAIN or (use_domain and store.domain and custom_domain_allowed(store)):
        return url
    return f"{url}{'&' if '?' in path else '?'}store={store.slug}"


def custom_domain_allowed(store):
    from apps.billing.plans import effective_plan
    plan = effective_plan(store)
    return plan is None or plan.allow_custom_domain
