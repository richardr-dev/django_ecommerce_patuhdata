from django.urls import path

from . import views

urlpatterns = [
    path("books/settlements/", views.settlement_list, name="settlement-list"),
    path("books/settlements/new/", views.settlement_new, name="settlement-new"),
    path("books/settlements/<uuid:pk>/", views.settlement_detail, name="settlement-detail"),
    path("books/mitra/", views.settlement_partners, name="settlement-partners"),
    path("books/mitra/stok/", views.partner_stock, name="partner-stock"),
    path("books/mitra/<int:pk>/", views.settlement_partner_detail, name="settlement-partner-detail"),
    path("books/products/<int:product_id>/partner-prices/", views.product_partner_prices, name="product-partner-prices"),
    path("books/mitra/<int:partner_id>/invoices/", views.mitra_invoice_list, name="mitra-invoice-list"),
    path("books/mitra/<int:partner_id>/invoices/new/", views.mitra_invoice_new, name="mitra-invoice-new"),
]
