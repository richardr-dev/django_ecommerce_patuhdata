from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from config.ratelimit import rate_limit

from . import setup_views, views
from .forms import CustomerLoginForm

# Per IP and per submitted username/email; see config/ratelimit.py.
login_limit = rate_limit("login", limit=10, window=900, field="username")

urlpatterns = [
    path("merchant/quick-defaults/", setup_views.merchant_quick_defaults, name="merchant-quick-defaults"),
    path("merchant/products/import/", setup_views.merchant_product_import, name="merchant-product-import"),
    path("merchant/products/import/template/", setup_views.merchant_product_import_template, name="merchant-product-import-template"),
    path("account/login/", login_limit(auth_views.LoginView.as_view(
        template_name="registration/login.html", authentication_form=CustomerLoginForm)), name="login"),
    path("account/logout/", auth_views.LogoutView.as_view(next_page="/"), name="logout"),
    path("account/register/", rate_limit("register", limit=5, window=3600)(views.customer_register),
        name="register"),
    path("account/orders/", views.account_orders, name="account-orders"),
    path("account/password-reset/", rate_limit("password-reset", limit=5, window=3600, field="email")(
        auth_views.PasswordResetView.as_view(
        template_name="registration/password_reset_form.html",
        email_template_name="registration/password_reset_email.txt",
        subject_template_name="registration/password_reset_subject.txt",
        success_url=reverse_lazy("password-reset-done"))), name="password-reset"),
    path("account/password-reset/done/", auth_views.PasswordResetDoneView.as_view(
        template_name="registration/password_reset_done.html"), name="password-reset-done"),
    path("account/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="registration/password_reset_confirm.html",
        success_url=reverse_lazy("password-reset-complete")), name="password-reset-confirm"),
    path("account/reset/complete/", auth_views.PasswordResetCompleteView.as_view(
        template_name="registration/password_reset_complete.html"), name="password-reset-complete"),
    path("merchant/login/", login_limit(auth_views.LoginView.as_view(
        template_name="registration/merchant_login.html")), name="merchant-login"),
    path("checkout/shipping-rates/", views.shipping_rates, name="shipping-rates"),
    path("checkout/shipping-areas/", views.shipping_areas, name="shipping-areas"),
    path("cart/", views.cart_view, name="cart"),
    path("cart/recovery/", rate_limit("cart-recovery", limit=10, window=3600)(
        views.cart_recovery_opt_in), name="cart-recovery-opt-in"),
    path("cart/recover/<uuid:token>/", views.cart_recover, name="cart-recover"), path("cart/add/<int:product_id>/", views.cart_add, name="cart-add"),
    path("cart/update/<str:item_key>/", views.cart_update, name="cart-update"),
    path("checkout/", rate_limit("checkout", limit=30, window=600)(views.checkout), name="checkout"), path("orders/<uuid:order_id>/", views.order_detail, name="order-detail"),
    path("orders/<uuid:order_id>/return/", views.request_return, name="request-return"),
    path("merchant/", views.merchant_dashboard, name="merchant-dashboard"),
    path("merchant/orders/", views.merchant_orders, name="merchant-orders"),
    path("merchant/stock/", views.merchant_stock, name="merchant-stock"),
    path("merchant/orders.csv", views.merchant_orders_csv, name="merchant-orders-csv"),
    path("merchant/subscribers.csv", views.merchant_subscribers_csv, name="merchant-subscribers-csv"),
    path("merchant/orders/<uuid:order_id>/update/", views.merchant_order_update, name="merchant-order-update"),
    path("merchant/orders/<uuid:order_id>/tracking/", views.merchant_order_tracking,
        name="merchant-order-tracking"),
    path("merchant/orders/<uuid:order_id>/book-shipment/", views.merchant_order_book_shipment,
        name="merchant-order-book-shipment"),
    path("merchant/orders/<uuid:order_id>/refund/", views.merchant_order_refund,
        name="merchant-order-refund"),
    path("merchant/settings/", views.merchant_settings, name="merchant-settings"),
    path("merchant/settings/template/", views.merchant_apply_template, name="merchant-apply-template"),
    path("merchant/customers/", views.merchant_customers, name="merchant-customers"),
    path("merchant/customers/<int:customer_id>/", views.merchant_customer_detail, name="merchant-customer-detail"),
    path("merchant/integrations/", views.merchant_integrations, name="merchant-integrations"),
    path("merchant/engagement/", views.merchant_engagement, name="merchant-engagement"),
    path("merchant/integrations/<int:event_id>/retry/", views.merchant_integration_retry, name="merchant-integration-retry"),
    path("merchant/setup/<slug:section>/", views.merchant_entity_list, name="merchant-entity-list"),
    path("merchant/setup/<slug:section>/new/", views.merchant_entity_edit, name="merchant-entity-new"),
    path("merchant/setup/<slug:section>/<int:object_id>/", views.merchant_entity_edit, name="merchant-entity-edit"),
]
