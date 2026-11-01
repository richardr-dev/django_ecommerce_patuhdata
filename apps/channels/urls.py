from django.urls import path

from . import views

urlpatterns = [
    path("merchant/marketplace/", views.merchant_channels, name="merchant-channels"),
    path("merchant/marketplace/template.csv", views.channel_orders_template,
        name="merchant-channel-template"),
    path("merchant/marketplace/<int:channel_id>/", views.merchant_channel_detail,
        name="merchant-channel-detail"),
    path("merchant/marketplace/<int:channel_id>/stock.csv", views.channel_stock_csv,
        name="merchant-channel-stock"),
]
