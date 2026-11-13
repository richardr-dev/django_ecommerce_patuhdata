from django.urls import path

from . import activity, campaign_views, install_views, shortlinks, staff_views, tracking, views

urlpatterns = [
    path("instalasi/", install_views.partner_install, name="partner-install"),
    path("favicon.ico", views.favicon, name="favicon"),
    # /s/ is a seller's own campaign short link.
    path("s/<slug:code>", shortlinks.campaign_redirect, name="campaign-short"),
    path("s/<slug:code>/", shortlinks.campaign_redirect, name="campaign-short-slash"),
    path("merchant/staff/", staff_views.merchant_staff, name="merchant-staff"),
    path("merchant/staff/akses/", staff_views.merchant_access, name="merchant-access"),
    path("merchant/activity/", activity.merchant_activity, name="merchant-activity"),
    path("merchant/tracking/", tracking.merchant_tracking, name="merchant-tracking"),
    path("merchant/staff/<int:staff_id>/", staff_views.merchant_staff_update,
        name="merchant-staff-update"),
    path("merchant/invite/<str:uidb64>/<str:token>/", staff_views.staff_invite_accept,
        name="staff-invite-accept"),
    path("merchant/campaign-links/", campaign_views.campaign_links, name="merchant-campaign-links"),
    path("merchant/feeds/", campaign_views.feeds_and_qr, name="merchant-feeds"),
    path("merchant/campaign-links/<int:link_id>/toggle/", campaign_views.toggle_campaign_link,
        name="merchant-campaign-link-toggle"),
]
