"""Platform plumbing that is not a page of its own: the favicon and Caddy's TLS check.

Stores are not opened from the web: a partner installs them (``provisioning.py``, ``/instalasi/``)."""
from django.http import HttpResponse
from django.shortcuts import redirect
from django.templatetags.static import static

from .models import Store
from .tenancy import custom_domain_allowed, platform_host, subdomain_slug


def favicon(request):
    """Browsers and crawlers request /favicon.ico on every host, platform and store alike.
    Temporary redirect: the target's hashed name changes whenever the icon does."""
    return redirect(static("img/brand/favicon.ico"))


class TLSAskMiddleware:
    """Answer Caddy's on-demand TLS check before host validation: Caddy calls web:8000.

    A certificate is issued only for the platform host, an active store's subdomain, or a
    custom domain whose plan allows it, so strangers cannot make the server request
    certificates for arbitrary names."""
    path = "/internal/tls-ask/"

    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        if request.path != self.path:
            return self.get_response(request)
        domain = request.GET.get("domain", "").strip().lower().rstrip(".")
        return HttpResponse(status=200 if domain and _serves(domain) else 404)


def _serves(domain):
    if platform_host(domain):
        return True
    slug = subdomain_slug(domain)
    if slug:
        return Store.objects.filter(slug=slug, is_active=True).exists()
    store = Store.objects.filter(domain=domain, is_active=True).select_related(
        "subscription__plan").first()
    return bool(store) and custom_domain_allowed(store)
