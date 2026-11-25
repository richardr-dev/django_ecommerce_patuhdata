"""What a plan opens in the back office.

Starter is the marketplace package for UMKM: orders and invoices from the marketplaces, products and stock,
customers and settings. A plan without ``Plan.full_books`` gets that set only; the accounting, purchasing,
cash and bank, tax, mitra and POS pages are hidden from the sidebar and the "Buat baru" menu and answer with
an upgrade notice. Nothing is deleted or switched off underneath: the ledger keeps posting, so an upgrade
shows complete books at once.

A page is judged by the sidebar link that claims it most specifically (the same rule that lights the
sidebar), so a detail page follows its list. A page no link claims (Beranda, the invoice form...) is open."""
from functools import lru_cache

from django.urls import NoReverseMatch, reverse

# Sidebar groups a Starter company keeps.
STARTER_GROUPS = frozenset({"Penjualan", "Stok & Produk", "Marketplace", "Pelanggan", "Pengaturan",
    "Toko online", "Promosi"})
# Links inside those groups that are still for the full package.
STARTER_LOCKED_LINKS = frozenset({"merchant-quotes", "merchant-sales-orders", "merchant-deliveries",
    "merchant-pos", "merchant-price-lists", "merchant-production-recap", "merchant-units", "merchant-lots",
    "merchant-stock-counts", "merchant-stock-requests", "merchant-stock-minus"})
# Links in locked groups that serve the marketplace seller and stay open.
STARTER_OPEN_LINKS = frozenset({"merchant-channel-recap", "merchant-sales-overview", "merchant-sales-export"})
# Whole areas outside the sidebar that belong to locked modules.
STARTER_LOCKED_PREFIXES = ("/kasir/",)


def full_books(store):
    """True when the company's plan includes everything (no subscription: an operator-managed store)."""
    if store is None:
        return True
    from .plans import effective_plan
    plan = effective_plan(store)
    return plan is None or getattr(plan, "full_books", True)


def link_open(group_label, link):
    if link.url in STARTER_OPEN_LINKS:
        return True
    return group_label in STARTER_GROUPS and link.url not in STARTER_LOCKED_LINKS


@lru_cache(maxsize=1)
def _rules():
    from apps.orders.templatetags.merchant_nav import NAV
    rules = []
    for _title, groups in NAV:
        for group in groups:
            for link in group.links:
                try:
                    href = reverse(link.url, args=link.args)
                except NoReverseMatch:
                    continue
                rules.append((link, href, link_open(group.label, link)))
    return tuple(rules)


def path_open(path):
    """Whether a Starter company may open ``path``."""
    if path.startswith(STARTER_LOCKED_PREFIXES):
        return False
    from apps.orders.templatetags.merchant_nav import _score
    best, allowed = 0, True
    for link, href, is_open in _rules():
        if link.query:      # a filtered view of a list another link already claims
            continue
        score = _score(link, path, href, {})
        if score > best:
            best, allowed = score, is_open
    return allowed


def allows(store, path):
    return full_books(store) or path_open(path)


class PlanModulesMiddleware:
    """A Starter company opening a page of a module its plan lacks gets the upgrade notice (status 402, so a
    soft navigation falls back to a full page and a form post or htmx call changes nothing). Runs after the
    trial lock, which already answers for an expired company."""
    def __init__(self, get_response): self.get_response = get_response

    def __call__(self, request):
        path = request.path
        user = getattr(request, "user", None)
        if (path.startswith(("/books/", "/kasir/")) and user is not None and user.is_authenticated
                and not user.is_superuser and not path_open(path)):
            from apps.shops.trial import _store_of, sales_contact
            store = _store_of(request)
            if store is not None and not full_books(store):
                from django.shortcuts import render

                from apps.shops.permissions import roles

                from .plans import effective_plan
                role = "admin" if store.owner_id == user.id else getattr(
                    store.staff_members.filter(user=user).first(), "role", "")
                return render(request, "platform/plan_locked.html", {"store": store,
                    "plan": effective_plan(store), "contact": sales_contact(),
                    "can_pay": role in roles("billing.manage")}, status=402)
        return self.get_response(request)
