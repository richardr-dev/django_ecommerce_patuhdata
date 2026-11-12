from django.apps import AppConfig


class TaxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tax"

    def ready(self):
        from . import posting  # noqa: F401  registers its ledger backlog
