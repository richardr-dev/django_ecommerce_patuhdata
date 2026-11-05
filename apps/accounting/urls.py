from django.urls import path

from . import expenses, views

urlpatterns = [
    path("merchant/expenses/", expenses.merchant_expenses, name="merchant-expenses"),
    path("merchant/expenses/<int:expense_id>/delete/", expenses.merchant_expense_delete,
        name="merchant-expense-delete"),
    path("merchant/expenses/<int:expense_id>/receipt/", expenses.merchant_expense_receipt,
        name="merchant-expense-receipt"),
    path("merchant/payouts/", views.merchant_payouts, name="merchant-payouts"),
    path("merchant/payouts/template.csv", views.payout_template, name="merchant-payout-template"),
    path("merchant/payouts/<int:payout_id>/", views.merchant_payout_detail,
        name="merchant-payout-detail"),
]
