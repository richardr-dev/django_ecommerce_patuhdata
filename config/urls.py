from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from .views import health

urlpatterns = [
    path("healthz/", health, name="health"),
    path("platform-admin/", admin.site.urls),
    path("integrations/", include("apps.integrations.urls")),
    path("api/v1/", include("apps.orders.api_urls")),
    path("", include("apps.orders.urls")),
    path("", include("apps.catalog.urls")),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
