from django.urls import path

from .views import demo_payment, doku_webhook, xendit_webhook

urlpatterns = [
    path("demo-pay/<uuid:order_id>/", demo_payment, name="demo-payment"),
    path("webhooks/xendit/", xendit_webhook, name="xendit-webhook"),
    path("webhooks/doku/", doku_webhook, name="doku-webhook"),
]
