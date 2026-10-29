from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("privacy/", views.privacy_policy, name="privacy-policy"),
    path("robots.txt", views.robots_txt, name="robots-txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap"),
    path("pages/<slug:slug>/", views.store_page, name="store-page"),
    path("products/<slug:slug>/", views.product_detail, name="product-detail"),
    path("products/<slug:slug>/review/", views.product_review, name="product-review"),
    path("wishlist/<int:product_id>/", views.wishlist_toggle, name="wishlist-toggle"),
    path("account/wishlist/", views.account_wishlist, name="account-wishlist"),
    path("newsletter/subscribe/", views.newsletter_subscribe, name="newsletter-subscribe"),
    path("newsletter/unsubscribe/<uuid:token>/", views.newsletter_unsubscribe,
        name="newsletter-unsubscribe"),
]
