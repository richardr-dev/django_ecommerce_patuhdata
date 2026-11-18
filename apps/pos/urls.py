from django.urls import path

from . import views

urlpatterns = [
    path("struk/<str:token>/", views.public_receipt, name="pos-receipt-public"),
    path("kasir/", views.kasir_home, name="pos-app"),
    path("kasir/login/", views.PosLoginView.as_view(), name="pos-login"),
    path("kasir/keluar/", views.pos_logout, name="pos-logout"),
    path("books/pos/", views.merchant_pos, name="merchant-pos"),
    path("books/pos/register/new/", views.merchant_pos_register_new, name="merchant-pos-register-new"),
    path("books/pos/outlet/<int:outlet_id>/settings/", views.merchant_pos_settings, name="merchant-pos-settings"),
    path("books/pos/<int:register_id>/open/", views.merchant_pos_open, name="merchant-pos-open"),
    path("books/pos/<int:register_id>/sell/", views.merchant_pos_sell, name="merchant-pos-sell"),
    path("books/pos/shift/<int:shift_id>/", views.merchant_pos_shift, name="merchant-pos-shift"),
    path("books/pos/shift/<int:shift_id>/paid-out/", views.merchant_pos_paid_out, name="merchant-pos-paid-out"),
    path("books/pos/shift/<int:shift_id>/paid-in/", views.merchant_pos_paid_in, name="merchant-pos-paid-in"),
    path("books/pos/receipt/<uuid:sale_id>/", views.merchant_pos_receipt, name="merchant-pos-receipt"),
    path("books/pos/receipt/<uuid:sale_id>/void/", views.merchant_pos_void, name="merchant-pos-void"),
    path("books/pos/receipt/<uuid:sale_id>/refund/", views.merchant_pos_refund, name="merchant-pos-refund"),
]
