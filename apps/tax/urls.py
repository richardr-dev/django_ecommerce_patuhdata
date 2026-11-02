from django.urls import path

from . import views

urlpatterns = [
    path("merchant/tax/", views.merchant_tax, name="merchant-tax"),
    path("merchant/tax/invoices.csv", views.merchant_invoice_register,
        name="merchant-invoice-register"),
    path("merchant/invoices/<uuid:invoice_id>/", views.merchant_invoice, name="merchant-invoice"),
    path("orders/<uuid:order_id>/invoice/", views.order_invoice, name="order-invoice"),
]
