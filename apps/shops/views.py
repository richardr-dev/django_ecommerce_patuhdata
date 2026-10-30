"""The platform's own pages: marketing home, store directory, seller signup, TLS check."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.text import slugify

from apps.billing.models import Plan, Subscription
from apps.billing.plans import default_plan
from apps.catalog.views import paginate
from apps.orders.setup_views import create_store_defaults

from .forms import StoreSignupForm
from .models import AuditLog, Organization, Store
from .tenancy import custom_domain_allowed, platform_host, platform_url, store_url, subdomain_slug

DIRECTORY_PAGE_SIZE = 24


def _directory_stores():
    """Stores a visitor can actually shop: active, opted in, with an active product."""
    return (Store.objects.filter(is_active=True, listed_in_directory=True,
        products__is_active=True).distinct().select_related("subscription__plan"))


def _with_urls(stores):
    return [{"store": store, "url": store_url(store)} for store in stores]


def platform_home(request):
    stores = _directory_stores().order_by("-created_at", "-id")[:8]
    return render(request, "platform/home.html", {"featured_stores": _with_urls(stores),
        "plans": Plan.objects.filter(is_public=True), "free_plan": default_plan()})


def store_directory(request):
    stores = _directory_stores().order_by("name", "id")
    query = request.GET.get("q", "").strip()
    if query:
        stores = stores.filter(Q(name__icontains=query) | Q(description__icontains=query))
    page, page_range, page_query = paginate(request, stores, DIRECTORY_PAGE_SIZE)
    return render(request, "platform/directory.html", {"stores": _with_urls(page.object_list),
        "page_obj": page, "page_range": page_range, "page_query": page_query, "query": query})


def store_signup(request):
    if request.store and settings.PLATFORM_DOMAIN:
        # Sign up on the platform host, so the new merchant session lives where the back office is.
        return redirect(platform_url("/start/"))
    form = StoreSignupForm(request.POST or None, user=request.user)
    plan = default_plan()
    if request.method == "POST" and form.is_valid():
        if not plan:
            form.add_error(None, "Pendaftaran toko baru belum dibuka. Hubungi pengelola platform.")
        else:
            try:
                user, store = _create_store(form, plan)
            except IntegrityError:
                form.add_error("slug", "Alamat ini baru saja dipakai. Pilih alamat lain.")
            else:
                if not request.user.is_authenticated:
                    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                request.session["merchant_store_id"] = store.id
                messages.success(request, f"Toko {store.name} sudah aktif di {store_url(store)}. "
                    "Lengkapi tampilan dan tambahkan produk pertama Anda.")
                return redirect("merchant-settings")
    return render(request, "platform/signup.html", {"form": form, "plan": plan,
        "platform_domain": settings.PLATFORM_DISPLAY_DOMAIN or settings.PLATFORM_DOMAIN})


@transaction.atomic
def _create_store(form, plan):
    data = form.cleaned_data
    user = form.user or get_user_model().objects.create_user(username=data["email"],
        email=data["email"], password=data["password1"], first_name=data["full_name"])
    org_slug, suffix = data["slug"], 2
    while Organization.objects.filter(slug=org_slug).exists():
        org_slug, suffix = f"{slugify(data['slug'])[:44]}-{suffix}", suffix + 1
    organization = Organization.objects.create(name=data["store_name"], slug=org_slug, owner=user)
    store = Store.objects.create(owner=user, organization=organization, name=data["store_name"],
        slug=data["slug"], support_phone=data["phone"], support_email=user.email,
        instagram=data.get("instagram", ""))
    Subscription.objects.create(store=store, plan=plan)
    create_store_defaults(store)
    AuditLog.objects.create(store=store, actor=user, action="store.created_by_signup",
        object_type="Store", object_id=str(store.id), details={"plan": plan.code})
    return user, store


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
