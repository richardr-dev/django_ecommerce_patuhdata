from django.db import connection
from django.http import JsonResponse

from apps.integrations.models import OutboxEvent


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        dead_events = OutboxEvent.objects.filter(status="failed", attempts__gte=8).count()
    except Exception:  # noqa: BLE001 - health boundary must convert infrastructure errors to 503
        return JsonResponse({"application": "degraded", "database": "degraded"}, status=503)
    status = 503 if dead_events else 200
    return JsonResponse({"application": "healthy" if status == 200 else "degraded",
        "database": "healthy", "outbox_dead_events": dead_events}, status=status)
