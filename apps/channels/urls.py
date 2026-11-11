from django.urls import path

from . import views

urlpatterns = [
    path("merchant/marketplace/", views.merchant_channels, name="merchant-channels"),
    path("merchant/sales/", views.merchant_sales, name="merchant-sales"),
    path("merchant/sales/stats/", views.merchant_sales_stats, name="merchant-sales-stats"),
    path("merchant/marketplace/import/", views.merchant_import, name="merchant-import"),
    path("merchant/marketplace/invoices/", views.merchant_sales_invoices, name="merchant-sales-invoices"),
    path("merchant/marketplace/buyers/", views.merchant_channel_buyers, name="merchant-channel-buyers"),
    path("merchant/marketplace/template.csv", views.channel_orders_template,
        name="merchant-channel-template"),
    path("merchant/marketplace/<int:channel_id>/", views.merchant_channel_detail,
        name="merchant-channel-detail"),
    path("merchant/marketplace/<int:channel_id>/stock.csv", views.channel_stock_csv,
        name="merchant-channel-stock"),
    path("merchant/sales/invoices/new/", views.merchant_invoice_new, name="merchant-invoice-new"),
    path("merchant/sales/invoices/batch/", views.merchant_invoice_batch, name="merchant-invoice-batch"),
    path("merchant/sales/invoices/<uuid:invoice_id>/", views.merchant_invoice_detail,
        name="merchant-invoice-detail"),
    path("merchant/sales/invoices/<uuid:invoice_id>/edit/", views.merchant_invoice_edit,
        name="merchant-invoice-edit"),
    path("merchant/sales/invoices/<uuid:invoice_id>/duplicate/", views.merchant_invoice_duplicate,
        name="merchant-invoice-duplicate"),
    path("merchant/sales/invoices/<uuid:invoice_id>/delete/", views.merchant_invoice_delete,
        name="merchant-invoice-delete"),
    path("merchant/sales/orders/<uuid:order_id>/issue-invoice/", views.merchant_invoice_issue,
        name="merchant-invoice-issue"),
    path("merchant/sales/invoices/export.csv", views.merchant_invoices_csv,
        name="merchant-invoices-csv"),
    path("merchant/sales/report/", views.merchant_sales_report, name="merchant-sales-report"),
    path("merchant/sales/channel-stock/", views.merchant_channel_stock, name="merchant-channel-stock"),
]
