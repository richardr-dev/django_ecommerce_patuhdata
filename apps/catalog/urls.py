from django.urls import path

from config.ratelimit import rate_limit

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("privacy/", views.privacy_policy, name="privacy-policy"),
    path("terms/", views.terms_of_service, name="terms-of-service"),
    path("robots.txt", views.robots_txt, name="robots-txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap"),
    path("pages/<slug:slug>/", views.store_page, name="store-page"),
    path("products/<slug:slug>/", views.product_detail, name="product-detail"),
    path("products/<slug:slug>/review/", rate_limit("review", limit=10, window=3600)(
        views.product_review), name="product-review"),
    path("wishlist/<int:product_id>/", views.wishlist_toggle, name="wishlist-toggle"),
    path("account/wishlist/", views.account_wishlist, name="account-wishlist"),
    path("newsletter/subscribe/", rate_limit("newsletter", limit=10, window=3600)(
        views.newsletter_subscribe), name="newsletter-subscribe"),
    path("newsletter/unsubscribe/<uuid:token>/", views.newsletter_unsubscribe,
        name="newsletter-unsubscribe"),
]
