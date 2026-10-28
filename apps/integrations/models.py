from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.shops.models import Store


class IntegrationCredential(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="integrations")
    provider = models.CharField(max_length=40)
    encrypted_config = models.JSONField(default=dict, help_text="Use AWS Secrets Manager in production; never store raw card data.")
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "provider"], name="unique_store_provider")]

class OutboxEvent(models.Model):
    MAX_ATTEMPTS = 8
    STATUS = [("pending", "Pending"), ("processing", "Processing"),
        ("succeeded", "Succeeded"), ("failed", "Failed"),
        ("dead", "Dead (needs manual retry)")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    topic = models.CharField(max_length=100)
    aggregate_id = models.CharField(max_length=160)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def schedule_retry(self, error):
        self.last_error = str(error)[:2000]
        self.last_attempt_at = timezone.now()
        if self.attempts >= self.MAX_ATTEMPTS:
            self.status, self.next_retry_at = "dead", None
        else:
            self.status = "failed"
            self.next_retry_at = timezone.now() + timedelta(minutes=min(60, 2 ** self.attempts))
        self.save(update_fields=["attempts", "status", "last_error", "last_attempt_at", "next_retry_at"])

    def requeue(self):
        """Give a dead or failed event a fresh set of attempts (manual retry)."""
        self.status, self.attempts, self.next_retry_at, self.last_error = "pending", 0, timezone.now(), ""
        self.save(update_fields=["status", "attempts", "next_retry_at", "last_error"])


class WorkerHeartbeat(models.Model):
    """Last time each background loop finished a cycle; surfaced by /healthz/ for alerting."""
    name = models.CharField(max_length=60, primary_key=True)
    last_seen_at = models.DateTimeField()
    last_error = models.TextField(blank=True)

    @classmethod
    def beat(cls, name, error=""):
        cls.objects.update_or_create(name=name, defaults={"last_seen_at": timezone.now(),
            "last_error": str(error)[:2000]})
