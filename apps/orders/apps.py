from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from .access import claim_session_orders

        def claim_orders(sender, request, user, **kwargs):
            if request is not None and hasattr(request, "session"):
                claim_session_orders(request, user)

        user_logged_in.connect(claim_orders, dispatch_uid="orders.claim_session_orders")
