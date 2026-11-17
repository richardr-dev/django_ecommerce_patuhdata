from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path

from config.ratelimit import rate_limit

from .views import health, legacy_merchant

urlpatterns = [
    path("healthz/", health, name="health"),
    path("platform-admin/login/", rate_limit("login", limit=10, window=900, field="username")(
        admin.site.login), name="platform-admin-login"),
    path("platform-admin/", admin.site.urls),
    path("integrations/", include("apps.integrations.urls")),
    path("api/v1/", include("apps.orders.api_urls")),
    path("", include("apps.billing.urls")),
    path("", include("apps.shops.urls")),
    path("", include("apps.orders.urls")),
    path("", include("apps.channels.urls")),
    path("", include("apps.tax.urls")),
    path("", include("apps.accounting.urls")),
    path("", include("apps.catalog.urls")),
    path("", include("apps.settlements.urls")),
    path("", include("apps.pos.urls")),
    # Last: the back office moved from /merchant/ to /books/, and the old addresses keep working.
    re_path(r"^merchant/(?P<rest>.*)$", legacy_merchant),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
