from django.urls import path

from . import views

urlpatterns = [
    path("checkout/shipping-rates/", views.shipping_rates, name="shipping-rates"),
    path("checkout/shipping-areas/", views.shipping_areas, name="shipping-areas"),
    path("cart/", views.cart_view, name="cart"), path("cart/add/<int:product_id>/", views.cart_add, name="cart-add"),
    path("cart/update/<str:item_key>/", views.cart_update, name="cart-update"),
    path("checkout/", views.checkout, name="checkout"), path("orders/<uuid:order_id>/", views.order_detail, name="order-detail"),
    path("orders/<uuid:order_id>/return/", views.request_return, name="request-return"),
    path("merchant/", views.merchant_dashboard, name="merchant-dashboard"),
    path("merchant/orders/", views.merchant_orders, name="merchant-orders"),
    path("merchant/orders.csv", views.merchant_orders_csv, name="merchant-orders-csv"),
    path("merchant/orders/<uuid:order_id>/update/", views.merchant_order_update, name="merchant-order-update"),
    path("merchant/settings/", views.merchant_settings, name="merchant-settings"),
    path("merchant/customers/", views.merchant_customers, name="merchant-customers"),
    path("merchant/customers/<int:customer_id>/", views.merchant_customer_detail, name="merchant-customer-detail"),
    path("merchant/integrations/", views.merchant_integrations, name="merchant-integrations"),
    path("merchant/integrations/<int:event_id>/retry/", views.merchant_integration_retry, name="merchant-integration-retry"),
    path("merchant/setup/<slug:section>/", views.merchant_entity_list, name="merchant-entity-list"),
    path("merchant/setup/<slug:section>/new/", views.merchant_entity_edit, name="merchant-entity-new"),
    path("merchant/setup/<slug:section>/<int:object_id>/", views.merchant_entity_edit, name="merchant-entity-edit"),
]
