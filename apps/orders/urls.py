from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from config.ratelimit import rate_limit

from . import delivery_views, setup_views, views
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
    path("account/addresses/", views.account_addresses, name="account-addresses"),
    path("account/addresses/<int:address_id>/", views.account_addresses, name="account-address-edit"),
    path("account/addresses/<int:address_id>/<slug:action>/", views.account_address_action,
        name="account-address-action"),
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
    path("orders/track/", rate_limit("track-order", limit=10, window=900, field="phone")(views.track_order),
        name="track-order"),
    path("orders/<uuid:order_id>/review/", rate_limit("order-review", limit=20, window=3600)(
        views.order_review), name="order-review"),
    path("orders/<uuid:order_id>/return/", views.request_return, name="request-return"),
    path("orders/<uuid:order_id>/received/", views.order_received, name="order-received"),
    path("checkout/delivery-slots/", views.delivery_slots, name="checkout-delivery-slots"),
    path("checkout/draft/", rate_limit("checkout-draft", limit=120, window=600)(
        views.checkout_draft), name="checkout-draft"),
    path("orders/<uuid:order_id>/buy-again/", views.order_buy_again, name="order-buy-again"),
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
    path("merchant/orders/<uuid:order_id>/advance/", views.merchant_order_advance,
        name="merchant-order-advance"),
    path("merchant/orders/<uuid:order_id>/ship/", views.merchant_order_ship,
        name="merchant-order-ship"),
    path("merchant/orders/labels/", views.merchant_order_labels, name="merchant-order-labels"),
    path("merchant/orders/<uuid:order_id>/courier-label/", views.merchant_order_courier_label,
        name="merchant-order-courier-label"),
    path("merchant/orders/courier-labels/", views.merchant_order_courier_labels,
        name="merchant-order-courier-labels"),
    path("merchant/delivery/", delivery_views.merchant_delivery, name="merchant-delivery"),
    path("merchant/delivery/recap/", delivery_views.merchant_production_recap,
        name="merchant-production-recap"),
    path("merchant/orders/<uuid:order_id>/confirm-transfer/", views.merchant_order_confirm_transfer,
        name="merchant-order-confirm-transfer"),
    path("merchant/orders/<uuid:order_id>/refund/", views.merchant_order_refund,
        name="merchant-order-refund"),
    path("merchant/settings/", views.merchant_settings, name="merchant-settings"),
    path("merchant/settings/template/", views.merchant_apply_template, name="merchant-apply-template"),
    path("merchant/customers/", views.merchant_customers, name="merchant-customers"),
    path("merchant/customers/<int:customer_id>/", views.merchant_customer_detail, name="merchant-customer-detail"),
    path("merchant/integrations/", views.merchant_integrations, name="merchant-integrations"),
    path("merchant/finance/", views.merchant_finance, name="merchant-finance"),
    path("merchant/payments/", views.merchant_payment_settings, name="merchant-payment-settings"),
    path("merchant/payments/test/", views.merchant_payment_test, name="merchant-payment-test"),
    path("merchant/payments/balance/", views.merchant_platform_balance,
        name="merchant-platform-balance"),
    path("merchant/engagement/", views.merchant_engagement, name="merchant-engagement"),
    path("merchant/integrations/<int:event_id>/retry/", views.merchant_integration_retry, name="merchant-integration-retry"),
    path("merchant/shipping-areas/", rate_limit("merchant-areas", limit=60, window=600,
        methods=("GET",))(views.merchant_shipping_areas), name="merchant-shipping-areas"),
    path("merchant/setup/<slug:section>/", views.merchant_entity_list, name="merchant-entity-list"),
    path("merchant/setup/<slug:section>/new/", views.merchant_entity_edit, name="merchant-entity-new"),
    path("merchant/setup/<slug:section>/<int:object_id>/", views.merchant_entity_edit, name="merchant-entity-edit"),
]
