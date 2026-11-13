"""Duplicate-submission guard for forms that create money documents.

A form carries a one-off key (``<input type="hidden" name="idem" value="{{ idem_key }}">``). The
first submit claims it; a retry, double-click or back-and-resubmit with the same key does not create
a second invoice, bill, receipt or payment but is sent to the first one.

    earlier = idempotency.claim(store, "bill", key)
    if earlier is not None:            # already ran (earlier == "" while it is still running)
        return redirect(...)
    try:
        bill = create_bill(...)
    except Exception:
        idempotency.release(store, "bill", key); raise
    idempotency.finish(store, "bill", key, bill.pk)
"""
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyKey

STALE = timedelta(minutes=2)  # a claim with no result this old belongs to a run that crashed


def new_key():
    return uuid.uuid4().hex


def claim(store, scope, key):
    """None when this is the first use of ``key`` (it is now claimed); otherwise the id the first
    run produced, or "" while it has not finished."""
    key = (key or "").strip()[:64]
    if not key:
        return None
    try:
        with transaction.atomic():
            IdempotencyKey.objects.create(store=store, scope=scope, key=key)
        return None
    except IntegrityError:
        record = IdempotencyKey.objects.filter(store=store, scope=scope, key=key).first()
        if record and not record.result_id and record.created_at < timezone.now() - STALE:
            record.delete()  # the first run died without finishing: let this one through
            return claim(store, scope, key)
        return (record.result_id if record else "") or ""


def finish(store, scope, key, result_id):
    if key:
        IdempotencyKey.objects.filter(store=store, scope=scope, key=key.strip()[:64]).update(
            result_id=str(result_id))


def release(store, scope, key):
    """The run failed: free the key so the person can correct the form and submit again."""
    if key:
        IdempotencyKey.objects.filter(store=store, scope=scope, key=key.strip()[:64], result_id="").delete()
