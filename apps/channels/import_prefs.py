"""Each person's saved defaults for Impor pesanan and Pesanan (``ImportPreference``)."""
from .models import ImportBatch, ImportPreference

# Statuses a person can import; the built-in choice is finished and cancelled orders.
STATES = ("completed", "shipped", "active", "cancelled")
DEFAULT_STATES = ("completed", "cancelled")


def get(user, store):
    """The person's saved defaults, or an unsaved row holding the built-in ones."""
    found = ImportPreference.objects.filter(user=user, store=store).first()
    return found or ImportPreference(user=user, store=store, states=list(DEFAULT_STATES))


def states_of(pref):
    return {s for s in (pref.states or DEFAULT_STATES) if s in STATES}


def save_import_defaults(user, store, *, states, category, soft, show_in_orders):
    pref = get(user, store)
    pref.states = [s for s in STATES if s in states]
    pref.category = category if category in {c for c, _ in ImportBatch.CAT_CHOICES} else "regular"
    pref.soft, pref.show_in_orders = bool(soft), bool(show_in_orders)
    pref.save()
    return pref


def save_orders_view(user, store, view):
    pref = get(user, store)
    pref.orders_view = view if view in {ImportPreference.VIEW_ALL, ImportPreference.VIEW_WEB} else "all"
    pref.save()
    return pref
