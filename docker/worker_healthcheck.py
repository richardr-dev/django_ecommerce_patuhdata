"""Docker health check for a background loop: healthy while its heartbeat is fresh.

    python docker/worker_healthcheck.py                    # the outbox worker (process_outbox)
    python docker/worker_healthcheck.py run_books 50400    # the books worker, stale after 14 hours
"""
import os
import sys
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

from django.utils import timezone

from apps.integrations.models import WorkerHeartbeat
from config.views import WORKER_STALE_AFTER

name = sys.argv[1] if len(sys.argv) > 1 else "process_outbox"
stale = timedelta(seconds=int(sys.argv[2])) if len(sys.argv) > 2 else WORKER_STALE_AFTER
heartbeat = WorkerHeartbeat.objects.filter(name=name).first()
sys.exit(0 if heartbeat and timezone.now() - heartbeat.last_seen_at < stale else 1)
