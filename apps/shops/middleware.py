from .models import Store


class StoreResolverMiddleware:
    """Resolve tenant by custom domain, then /?store=slug, then the first demo store."""
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        slug = request.GET.get("store") or request.session.get("store_slug")
        store = Store.objects.filter(domain=host, is_active=True).first()
        if not store and slug:
            store = Store.objects.filter(slug=slug, is_active=True).first()
        if not store and host in {"localhost", "127.0.0.1", "testserver"}:
            store = Store.objects.filter(is_active=True).first()
        request.store = store
        if store: request.session["store_slug"] = store.slug
        return self.get_response(request)

