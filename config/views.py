from datetime import timedelta

from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

from apps.integrations.models import OutboxEvent, WorkerHeartbeat

# The worker loop finishes a cycle every few seconds; anything older means it is stuck or down.
WORKER_STALE_AFTER = timedelta(minutes=5)


class HealthCheckMiddleware:
    """Answer /healthz/ before host validation, sessions and tenant lookup.

    Load balancers probe with the instance IP as Host (not in ALLOWED_HOSTS) and must get a
    clean 503, not a 400/500, when the database is unreachable.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz/":
            return health(request)
        return self.get_response(request)


def health(request):
    """Liveness for the load balancer plus alert fields for monitoring.

    Only a database failure returns 503: background problems (dead outbox events, a stopped
    worker) must alert someone, not take every healthy web server out of rotation.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        dead_events = OutboxEvent.objects.filter(status="dead").count()
        heartbeat = WorkerHeartbeat.objects.filter(name="process_outbox").first()
    except Exception:  # noqa: BLE001 - health boundary must convert infrastructure errors to 503
        return JsonResponse({"application": "degraded", "database": "degraded"}, status=503)
    worker_age = (timezone.now() - heartbeat.last_seen_at).total_seconds() if heartbeat else None
    alerts = []
    if dead_events:
        alerts.append("outbox_dead_events")
    if worker_age is None or worker_age > WORKER_STALE_AFTER.total_seconds():
        alerts.append("worker_stale")
    if heartbeat and heartbeat.last_error:
        alerts.append("worker_task_errors")
    return JsonResponse({"application": "healthy", "database": "healthy",
        "outbox_dead_events": dead_events,
        "worker_last_seen_seconds": round(worker_age) if worker_age is not None else None,
        "alerts": alerts})
