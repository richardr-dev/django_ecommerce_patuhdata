from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("privacy/", views.privacy_policy, name="privacy-policy"),
    path("products/<slug:slug>/", views.product_detail, name="product-detail"),
]
