from django.apps import AppConfig


class SettlementsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.settlements"
    verbose_name = "Settlements"

    def ready(self):
        from . import consignment  # noqa: F401  registers its ledger backlog
