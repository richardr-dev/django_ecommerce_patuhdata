from django.urls import path

from . import expenses, views

urlpatterns = [
    path("merchant/accounting/", views.merchant_accounting, name="merchant-accounting"),
    path("merchant/accounting/accounts/", views.merchant_chart_of_accounts, name="merchant-chart-of-accounts"),
    path("merchant/accounting/suppliers/", views.merchant_suppliers, name="merchant-suppliers"),
    path("merchant/accounting/suppliers/<int:supplier_id>/", views.merchant_supplier_detail, name="merchant-supplier-detail"),
    path("merchant/accounting/bills/", views.merchant_bills, name="merchant-bills"),
    path("merchant/accounting/bills/new/", views.merchant_bill_new, name="merchant-bill-new"),
    path("merchant/accounting/bills/<uuid:bill_id>/", views.merchant_bill_detail, name="merchant-bill-detail"),
    path("merchant/accounting/balance-sheet/", views.merchant_balance_sheet, name="merchant-balance-sheet"),
    path("merchant/accounting/trial-balance/", views.merchant_trial_balance, name="merchant-trial-balance"),
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
