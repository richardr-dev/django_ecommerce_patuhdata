from django.urls import path

from . import views

urlpatterns = [path("", views.home, name="home"), path("products/<slug:slug>/", views.product_detail, name="product-detail")]

