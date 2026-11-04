"""Whether a storefront is open to shoppers.

- "setup": the seller has not finished setting up (no way to pay, nothing to sell, or no way to
  get the order to the buyer). Shoppers see a "coming soon" page instead of a half-empty store.
- "closed": the subscription lapsed. Shoppers can still browse and follow their orders, but the
  store says it is temporarily closed and takes no new orders. They are never told why.
- "open": everything works.

The seller always sees their own store, through a signed preview link from the back office."""
from django.core import signing

PREVIEW_SALT = "storefront-preview"
PREVIEW_MAX_AGE = 24 * 3600
PREVIEW_SESSION_KEY = "storefront_preview"

# Always reachable, whatever the store's state: the back office, payment and courier
# callbacks, buyers' existing orders and accounts, and files.
ALWAYS_OPEN = ("/merchant/", "/platform-admin/", "/integrations/", "/internal/", "/static/",
    "/media/", "/healthz/", "/orders/", "/account/", "/api/", "/robots.txt", "/favicon.ico",
    "/pages/", "/privacy/", "/terms/", "/newsletter/unsubscribe/", "/cart/recover/")
# What a closed store cannot do: take new orders.
ORDERING = ("/cart/add/", "/cart/update/", "/checkout/")


def setup_steps(store):
    """The steps a seller must finish before shoppers see the store, done or not."""
    from apps.integrations.payments import payment_options
    from apps.integrations.shipping import shipping_origin
    from apps.orders.models import ShippingMethod

    can_ship = bool(shipping_origin(store)) or ShippingMethod.objects.filter(
        store=store, is_active=True).exists() or store.warehouses.filter(
        is_outlet=True, is_active=True, pickup_enabled=True).exists()
    return [
        {"key": "product", "label": "Tambah minimal satu produk aktif",
            "done": store.products.filter(is_active=True).exists()},
        {"key": "payment", "label": "Aktifkan pembayaran (transfer bank atau payment gateway)",
            "done": bool(payment_options(store))},
        {"key": "shipping", "label": "Isi lokasi gudang (kode pos) atau tarif pengiriman toko",
            "done": can_ship},
    ]


def is_lapsed(store):
    from apps.billing.plans import subscription_status

    status = subscription_status(store)
    return bool(status and status["lapsed"])


def store_state(store):
    if is_lapsed(store):
        return "closed"
    if not all(step["done"] for step in setup_steps(store)):
        return "setup"
    return "open"


def preview_token(store):
    return signing.dumps({"store": store.id}, salt=PREVIEW_SALT, compress=True)


def preview_url(store, path="/"):
    """The store as its seller sees it: works before the store is open to shoppers."""
    from .tenancy import store_url

    return store_url(store, f"{path}?preview={preview_token(store)}")


def read_preview_token(token):
    try:
        return signing.loads(token, salt=PREVIEW_SALT, max_age=PREVIEW_MAX_AGE)["store"]
    except (signing.BadSignature, KeyError, TypeError):
        return None


def is_previewing(request, store):
    """The seller (or their staff) opened the store from the back office in this browser."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and (store.owner_id == user.id
            or store.staff_members.filter(user=user).exists()):
        return True
    return store.id in request.session.get(PREVIEW_SESSION_KEY, [])
