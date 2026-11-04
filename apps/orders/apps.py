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

        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_save

        from .tokens import revoke_tokens_on_credential_change
        pre_save.connect(revoke_tokens_on_credential_change, sender=get_user_model(),
            dispatch_uid="orders.revoke_tokens_on_credential_change")
