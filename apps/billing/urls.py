from django.urls import path

from . import views

urlpatterns = [
    path("books/billing/", views.merchant_billing, name="merchant-billing"),
    path("books/billing/review/", views.merchant_billing_review, name="merchant-billing-review"),
    path("books/billing/cancel/", views.merchant_billing_cancel, name="merchant-billing-cancel"),
    path("books/billing/<uuid:invoice_id>/cancel/", views.merchant_billing_cancel_invoice,
        name="merchant-billing-cancel-invoice"),
    path("books/billing/checkout/", views.merchant_billing_checkout, name="merchant-billing-checkout"),
    path("books/billing/<uuid:invoice_id>/pay/", views.merchant_billing_pay,
        name="merchant-billing-pay"),
    path("books/billing/auto-renew/", views.merchant_billing_autorenew,
        name="merchant-billing-autorenew"),
    path("books/billing/outlets/", views.merchant_billing_outlets,
        name="merchant-billing-outlets"),
    path("books/billing/<uuid:invoice_id>/check/", views.merchant_billing_check,
        name="merchant-billing-check"),
    path("books/billing/<uuid:invoice_id>/demo-pay/", views.billing_demo_pay, name="billing-demo-pay"),
]
