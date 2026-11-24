from django.apps import AppConfig


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"

    def ready(self):
        from . import landed_costs, marketplace_adjustments, payables, stock_adjustments  # noqa: F401  register their ledger backlog
