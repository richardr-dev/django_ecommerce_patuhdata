"""Docker health check for the outbox worker: healthy while its heartbeat is fresh."""
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

from django.utils import timezone

from apps.integrations.models import WorkerHeartbeat
from config.views import WORKER_STALE_AFTER

heartbeat = WorkerHeartbeat.objects.filter(name="process_outbox").first()
sys.exit(0 if heartbeat and timezone.now() - heartbeat.last_seen_at < WORKER_STALE_AFTER else 1)
