from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.orders.models import CommerceSession


class Command(BaseCommand):
    help = "Delete expired anonymous commerce engagement data"

    def handle(self, *args, **options):
        days = getattr(settings, "ENGAGEMENT_RETENTION_DAYS", 90)
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = CommerceSession.objects.filter(user__isnull=True,
            last_seen_at__lt=cutoff).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired engagement records."))
