"""Write to the platform change log (``PlatformLog``). Services call ``log`` after a change succeeds."""
from .models import PlatformLog


def diff(before, after):
    """{field: [old, new]} for the values that differ."""
    return {key: [before.get(key, ""), after.get(key, "")] for key in after
        if str(before.get(key, "")) != str(after.get(key, ""))}


def log(actor, action, target_type, label, changes=None, store=None):
    who = actor if getattr(actor, "pk", None) else None
    return PlatformLog.objects.create(actor=who, actor_name=(who.username if who else "")[:150], action=action,
        target_type=target_type, target_label=str(label)[:200], store=store, changes=changes or {})
