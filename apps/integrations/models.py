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
    STATUS = [("pending", "Pending"), ("processing", "Processing"),
        ("succeeded", "Succeeded"), ("failed", "Failed")]
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
        self.status = "failed"
        self.last_error = str(error)[:2000]
        self.last_attempt_at = timezone.now()
        self.next_retry_at = timezone.now() + timedelta(minutes=min(60, 2 ** self.attempts))
        self.save(update_fields=["attempts", "status", "last_error", "last_attempt_at", "next_retry_at"])
