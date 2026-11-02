from django.urls import path

from . import views

urlpatterns = [
    path("merchant/payouts/", views.merchant_payouts, name="merchant-payouts"),
    path("merchant/payouts/template.csv", views.payout_template, name="merchant-payout-template"),
    path("merchant/payouts/<int:payout_id>/", views.merchant_payout_detail,
        name="merchant-payout-detail"),
]
