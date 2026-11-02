from django.urls import path

from config.ratelimit import rate_limit

from . import views

urlpatterns = [
    path("start/", rate_limit("store-signup", limit=10, window=3600)(views.store_signup),
        name="store-signup"),
    path("stores/", views.store_directory, name="store-directory"),
    path("favicon.ico", views.favicon, name="favicon"),
]
