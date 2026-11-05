"""Two kinds of short link, kept apart on purpose:

- lakuin.store/r/<store slug>: Ajak teman. A seller invites another seller to open a store on
  Lakuin; it opens the signup page with the invite (apps/billing/referrals.py).
- lakuin.store/s/<code>: Link kampanye. A seller promotes their own store; it opens a page
  of that store, and the order the shopper then places is counted for the link."""
import secrets
import time
from urllib.parse import urlencode

from django.db.models import F
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone

from .models import CampaignLink, Store
from .tenancy import platform_url, store_url

CAMPAIGN_PARAM = "lk"
SESSION_KEY = "campaign_link"
ATTRIBUTION_DAYS = 30
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read aloud and type


def new_code(length=6):
    for _ in range(20):
        code = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if not CampaignLink.objects.filter(code=code).exists():
            return code
    return new_code(length + 1)


def campaign_short_url(link):
    return platform_url(f"/s/{link.code}")


def campaign_target(link):
    """The store page the link opens, tagged so the store remembers the campaign and
    analytics tools see where the visit came from."""
    path = link.destination if link.destination.startswith("/") else "/"
    joiner = "&" if "?" in path else "?"
    query = urlencode({CAMPAIGN_PARAM: link.code, "utm_source": "lakuin",
        "utm_medium": "shortlink", "utm_campaign": link.code})
    return store_url(link.store, f"{path}{joiner}{query}")


def referral_redirect(request, code):
    from apps.billing.models import ReferralLink
    from apps.billing.referrals import clean_code

    store = Store.objects.filter(slug=clean_code(code), is_active=True).first()
    if store is None:
        return redirect(platform_url("/"))
    from apps.billing.referrals import SESSION_KEY
    request.session[SESSION_KEY] = store.slug  # kept even if the redirect is lost
    link, _ = ReferralLink.objects.get_or_create(store=store)
    ReferralLink.objects.filter(pk=link.pk).update(clicks=F("clicks") + 1,
        last_clicked_at=timezone.now())
    return redirect(platform_url(f"/start/?ref={store.slug}"))


def campaign_redirect(request, code):
    link = CampaignLink.objects.select_related("store").filter(code=code.lower(),
        is_active=True, store__is_active=True).first()
    if link is None:
        raise Http404
    CampaignLink.objects.filter(pk=link.pk).update(clicks=F("clicks") + 1,
        last_clicked_at=timezone.now())
    return redirect(campaign_target(link))


def remember_campaign(request):
    """On a store page opened with ?lk=<code>, remember that campaign for this shopper."""
    code = request.GET.get(CAMPAIGN_PARAM, "").strip().lower()
    store = getattr(request, "store", None)
    if not code or store is None or len(code) > 40:
        return
    link_id = CampaignLink.objects.filter(code=code, store=store).values_list("id", flat=True).first()
    if link_id:
        request.session[SESSION_KEY] = {"id": link_id, "store": store.id, "at": int(time.time())}


def attributed_campaign_id(request):
    """The campaign link an order placed now should be counted for (last click, 30 days)."""
    value = request.session.get(SESSION_KEY) or {}
    store = getattr(request, "store", None)
    if not store or value.get("store") != store.id:
        return None
    if time.time() - value.get("at", 0) > ATTRIBUTION_DAYS * 86400:
        return None
    return value.get("id")
