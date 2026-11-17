from django.apps import AppConfig


class PosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.pos"
    verbose_name = "Kasir (POS)"

    def ready(self):
        from . import services  # noqa: F401  registers its ledger backlog
