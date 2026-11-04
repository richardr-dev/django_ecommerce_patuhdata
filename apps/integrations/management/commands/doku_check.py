"""Check the platform DOKU account before going live:

    python manage.py doku_check

Confirms the keys are accepted by opening a Rp10.000 test payment page (nothing is charged
unless someone pays it) and prints the Notification URL to enter in DOKU Back Office."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.integrations.http import ProviderError
from apps.integrations.payments import DokuPaymentProvider, doku_environment


class Command(BaseCommand):
    help = "Verify the platform DOKU credentials and show the notification URL."

    def handle(self, *args, **options):
        notification_url = settings.PUBLIC_BASE_URL + reverse("doku-webhook")
        self.stdout.write(f"Lingkungan    : {doku_environment()} ({settings.DOKU_API_URL})")
        self.stdout.write(f"Provider aktif: {settings.PAYMENT_PROVIDER}")
        self.stdout.write(f"Notification URL untuk DOKU Back Office: {notification_url}")
        if not notification_url.startswith("https://"):
            self.stdout.write(self.style.WARNING(
                "PUBLIC_BASE_URL bukan https: DOKU tidak bisa mengirim notifikasi ke sini."))
        try:
            url = DokuPaymentProvider().check_credentials()
        except ProviderError as error:
            raise CommandError(f"DOKU gagal: {error}") from error
        self.stdout.write(self.style.SUCCESS(f"Kredensial diterima. Halaman tes: {url}"))
        if settings.PAYMENT_PROVIDER != "doku":
            self.stdout.write(self.style.WARNING(
                "Set PAYMENT_PROVIDER=doku agar langganan dan checkout memakai DOKU."))
