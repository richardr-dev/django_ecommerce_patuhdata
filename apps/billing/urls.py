from django.urls import path

from . import views

urlpatterns = [
    path("merchant/billing/", views.merchant_billing, name="merchant-billing"),
    path("merchant/billing/checkout/", views.merchant_billing_checkout, name="merchant-billing-checkout"),
    path("merchant/billing/<uuid:invoice_id>/check/", views.merchant_billing_check,
        name="merchant-billing-check"),
    path("merchant/billing/<uuid:invoice_id>/demo-pay/", views.billing_demo_pay, name="billing-demo-pay"),
]
