from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security)
def doku_notification_url_check(app_configs, **kwargs):
    if settings.PAYMENT_PROVIDER != "doku":
        return []
    public_url = settings.PUBLIC_BASE_URL.lower()
    if (public_url.startswith("https://") and "localhost" not in public_url and
            "127.0.0.1" not in public_url):
        return []
    return [Warning(
        "DOKU cannot deliver payment notifications to the configured PUBLIC_BASE_URL.",
        hint=("Use a public HTTPS domain and configure "
            "https://YOUR_DOMAIN/integrations/webhooks/doku/ as the Notification URL "
            "for every enabled channel in DOKU Back Office."),
        id="integrations.W001",
    )]


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"
