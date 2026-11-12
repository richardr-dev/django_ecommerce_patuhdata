from django.apps import AppConfig


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"

    def ready(self):
        from . import payables  # noqa: F401  registers its ledger backlog
