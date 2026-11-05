from django.urls import path

from config.ratelimit import rate_limit

from . import campaign_views, shortlinks, views

urlpatterns = [
    path("start/", rate_limit("store-signup", limit=10, window=3600)(views.store_signup),
        name="store-signup"),
    path("stores/", views.store_directory, name="store-directory"),
    path("favicon.ico", views.favicon, name="favicon"),
    # Short links: /r/ invites a seller to Lakuin (Ajak teman); /s/ is a seller's own campaign.
    path("r/<slug:code>", shortlinks.referral_redirect, name="referral-short"),
    path("r/<slug:code>/", shortlinks.referral_redirect, name="referral-short-slash"),
    path("s/<slug:code>", shortlinks.campaign_redirect, name="campaign-short"),
    path("s/<slug:code>/", shortlinks.campaign_redirect, name="campaign-short-slash"),
    path("ajak-teman/", views.referral_program, name="referral-program"),
    path("merchant/campaign-links/", campaign_views.campaign_links, name="merchant-campaign-links"),
    path("merchant/campaign-links/<int:link_id>/toggle/", campaign_views.toggle_campaign_link,
        name="merchant-campaign-link-toggle"),
]
