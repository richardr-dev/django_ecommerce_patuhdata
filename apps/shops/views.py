"""The platform's own pages: marketing home, store directory, seller signup, TLS check."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils.text import slugify

from apps.billing.models import Plan, Subscription
from apps.billing.plans import default_plan, trial_end_for
from apps.catalog.views import _json_ld, paginate
from apps.orders.setup_views import create_store_defaults

from .availability import store_state
from .forms import StoreSignupForm
from .models import AuditLog, Organization, Store
from .tenancy import custom_domain_allowed, platform_host, platform_url, store_url, subdomain_slug

DIRECTORY_PAGE_SIZE = 24


def _directory_stores():
    """Stores a visitor can actually shop: active, opted in, with an active product."""
    return (Store.objects.filter(is_active=True, listed_in_directory=True,
        products__is_active=True).distinct().select_related("subscription__plan"))


def _open_only(stores):
    """Directory listings show stores shoppers can buy from now, not ones still being set up
    or closed. Checked per store, which is fine while the directory is small."""
    if not settings.STOREFRONT_GATE:
        return list(stores)
    return [store for store in stores if store_state(store) == "open"]


def _with_urls(stores):
    return [{"store": store, "url": store_url(store)} for store in stores]


def platform_home(request):
    stores = _open_only(_directory_stores().order_by("-created_at", "-id")[:40])[:8]
    # Tells search engines that the platform is a service of the operating company.
    organization = {"@context": "https://schema.org", "@type": "Organization",
        "name": settings.PLATFORM_COMPANY_NAME, "url": settings.PLATFORM_COMPANY_URL,
        "brand": {"@type": "Brand", "name": settings.PLATFORM_NAME,
            "slogan": settings.PLATFORM_TAGLINE, "url": platform_url("/"),
            "logo": platform_url(static("img/brand/icon-512.png"))}}
    if settings.PLATFORM_SUPPORT_EMAIL:
        organization["email"] = settings.PLATFORM_SUPPORT_EMAIL
    return render(request, "platform/home.html", {"featured_stores": _with_urls(stores),
        "plans": Plan.objects.filter(is_public=True), "free_plan": default_plan(),
        "trial_days": settings.TRIAL_DAYS,
        "platform_domain": settings.PLATFORM_DISPLAY_DOMAIN or settings.PLATFORM_DOMAIN,
        "organization_json_ld": _json_ld(organization)})


def referral_program(request):
    """Ajak teman: how the seller referral program works and its rules (platform page)."""
    if settings.PLATFORM_DOMAIN and getattr(request, "store", None):
        return redirect(platform_url("/ajak-teman/"))  # opened on a store's address
    from apps.billing.referrals import discount_percent
    return render(request, "platform/referral.html", {"percent": discount_percent(),
        "trial_days": settings.TRIAL_DAYS})


def favicon(request):
    """Browsers and crawlers request /favicon.ico on every host, platform and store alike.
    Temporary redirect: the target's hashed name changes whenever the icon does."""
    return redirect(static("img/brand/favicon.ico"))


def store_directory(request):
    stores = _directory_stores().order_by("name", "id")
    query = request.GET.get("q", "").strip()
    if query:
        stores = stores.filter(Q(name__icontains=query) | Q(description__icontains=query))
    page, page_range, page_query = paginate(request, _open_only(stores), DIRECTORY_PAGE_SIZE)
    return render(request, "platform/directory.html", {"stores": _with_urls(page.object_list),
        "page_obj": page, "page_range": page_range, "page_query": page_query, "query": query})


def store_signup(request):
    if request.store and settings.PLATFORM_DOMAIN:
        # Sign up on the platform host, so the new merchant session lives where the back office is.
        return redirect(platform_url("/start/"))
    from apps.billing.referrals import SESSION_KEY, clean_code, discount_percent, referrer_for

    if clean_code(request.GET.get("ref")):
        request.session[SESSION_KEY] = clean_code(request.GET["ref"])
    referrer = referrer_for(request.session.get(SESSION_KEY))
    form = StoreSignupForm(request.POST or None, user=request.user)
    plan = default_plan()
    if request.method == "POST" and form.is_valid():
        if not plan:
            form.add_error(None, "Pendaftaran toko baru belum dibuka. Hubungi pengelola platform.")
        else:
            try:
                user, store = _create_store(form, plan, referrer)
            except IntegrityError:
                form.add_error("slug", "Alamat ini baru saja dipakai. Pilih alamat lain.")
            else:
                request.session.pop(SESSION_KEY, None)
                if not request.user.is_authenticated:
                    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                request.session["merchant_store_id"] = store.id
                messages.success(request, f"Toko {store.name} sudah aktif di {store_url(store)}. "
                    "Lengkapi tampilan dan tambahkan produk pertama Anda.")
                return redirect("merchant-settings")
    return render(request, "platform/signup.html", {"form": form, "plan": plan,
        "referrer": referrer, "referral_percent": discount_percent(),
        "trial_days": settings.TRIAL_DAYS,
        "platform_domain": settings.PLATFORM_DISPLAY_DOMAIN or settings.PLATFORM_DOMAIN})


@transaction.atomic
def _create_store(form, plan, referrer=None):
    data = form.cleaned_data
    user = form.user or get_user_model().objects.create_user(username=form.login_name(),
        email=data["email"], password=data["password1"], first_name=data["full_name"])
    org_slug, suffix = data["slug"], 2
    while Organization.objects.filter(slug=org_slug).exists():
        org_slug, suffix = f"{slugify(data['slug'])[:44]}-{suffix}", suffix + 1
    organization = Organization.objects.create(name=data["store_name"], slug=org_slug, owner=user)
    store = Store.objects.create(owner=user, organization=organization, name=data["store_name"],
        slug=data["slug"], support_phone=data["phone"], support_email=user.email or "",
        instagram=data.get("instagram", ""))
    Subscription.objects.create(store=store, plan=plan, current_period_end=trial_end_for(plan))
    create_store_defaults(store)
    if referrer is not None:
        from apps.billing.referrals import record_signup
        record_signup(referrer, store, user)
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
