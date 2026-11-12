from django.urls import path

from . import views

urlpatterns = [
    path("merchant/settlements/", views.settlement_list, name="settlement-list"),
    path("merchant/settlements/new/", views.settlement_new, name="settlement-new"),
    path("merchant/settlements/<uuid:pk>/", views.settlement_detail, name="settlement-detail"),
    path("merchant/mitra/", views.settlement_partners, name="settlement-partners"),
    path("merchant/mitra/stok/", views.partner_stock, name="partner-stock"),
    path("merchant/mitra/<int:pk>/", views.settlement_partner_detail, name="settlement-partner-detail"),
    path("merchant/products/<int:product_id>/partner-prices/", views.product_partner_prices, name="product-partner-prices"),
    path("merchant/mitra/<int:partner_id>/invoices/", views.mitra_invoice_list, name="mitra-invoice-list"),
    path("merchant/mitra/<int:partner_id>/invoices/new/", views.mitra_invoice_new, name="mitra-invoice-new"),
]
