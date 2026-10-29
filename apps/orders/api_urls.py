from django.urls import path

from config.ratelimit import rate_limit

from . import api_views

urlpatterns = [
    path("store/", api_views.store_detail, name="api-store"),
    path("products/", api_views.products, name="api-products"),
    path("products/<slug:slug>/", api_views.product_detail, name="api-product-detail"),
    path("auth/token/", rate_limit("api-login", limit=10, window=900)(api_views.create_token),
        name="api-token"),
    path("auth/logout/", api_views.revoke_token, name="api-logout"),
    path("me/", api_views.me, name="api-me"),
    path("orders/", api_views.account_orders, name="api-orders"),
    path("loyalty/", api_views.loyalty_wallet, name="api-loyalty"),
]
