"""Lifecycle of sign-ins beyond the browser session: mobile app tokens end when the password
changes or the account is deactivated (web sessions already end then, through Django's
session auth hash), and expired sessions and old tokens are deleted daily."""
from datetime import timedelta

from django.utils import timezone

from .models import MobileAccessToken

KEEP_ENDED_TOKENS_DAYS = 30


def revoke_user_tokens(user_id):
    return MobileAccessToken.objects.filter(user_id=user_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now())


def revoke_tokens_on_credential_change(sender, instance, update_fields=None, **kwargs):
    if not instance.pk:
        return
    if update_fields is not None and not {"password", "is_active"} & set(update_fields):
        return  # e.g. last_login on every sign-in
    previous = sender.objects.filter(pk=instance.pk).values("password", "is_active").first()
    if previous and (previous["password"] != instance.password
            or (previous["is_active"] and not instance.is_active)):
        revoke_user_tokens(instance.pk)


def prune_sign_ins():
    """Delete expired web sessions and mobile tokens that ended over 30 days ago."""
    from django.contrib.sessions.models import Session

    now = timezone.now()
    sessions, _ = Session.objects.filter(expire_date__lt=now).delete()
    cutoff = now - timedelta(days=KEEP_ENDED_TOKENS_DAYS)
    tokens, _ = (MobileAccessToken.objects.filter(expires_at__lt=cutoff)
        | MobileAccessToken.objects.filter(revoked_at__lt=cutoff)).delete()
    return sessions + tokens
