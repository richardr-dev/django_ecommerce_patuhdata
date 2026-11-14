from django.urls import path

from . import activity, campaign_views, install_views, shortlinks, staff_views, tracking, views

urlpatterns = [
    path("registration/", install_views.partner_install, name="partner-install"),
    path("favicon.ico", views.favicon, name="favicon"),
    # /s/ is a seller's own campaign short link.
    path("s/<slug:code>", shortlinks.campaign_redirect, name="campaign-short"),
    path("s/<slug:code>/", shortlinks.campaign_redirect, name="campaign-short-slash"),
    path("books/staff/", staff_views.merchant_staff, name="merchant-staff"),
    path("books/staff/akses/", staff_views.merchant_access, name="merchant-access"),
    path("books/activity/", activity.merchant_activity, name="merchant-activity"),
    path("books/tracking/", tracking.merchant_tracking, name="merchant-tracking"),
    path("books/staff/<int:staff_id>/", staff_views.merchant_staff_update,
        name="merchant-staff-update"),
    path("books/invite/<str:uidb64>/<str:token>/", staff_views.staff_invite_accept,
        name="staff-invite-accept"),
    path("books/campaign-links/", campaign_views.campaign_links, name="merchant-campaign-links"),
    path("books/feeds/", campaign_views.feeds_and_qr, name="merchant-feeds"),
    path("books/campaign-links/<int:link_id>/toggle/", campaign_views.toggle_campaign_link,
        name="merchant-campaign-link-toggle"),
]
