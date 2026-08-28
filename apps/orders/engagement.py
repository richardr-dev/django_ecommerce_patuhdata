import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F, Sum
from django.template.loader import render_to_string
from django.utils import timezone

from .models import CommerceSession, JourneyEvent

ANALYTICS_EVENTS = {"home_view", "search", "product_view"}


def _visitor_id(request):
    value = request.session.get("commerce_visitor_id")
    try:
        visitor_id = uuid.UUID(str(value))
    except (TypeError, ValueError):
        visitor_id = uuid.uuid4()
        request.session["commerce_visitor_id"] = str(visitor_id)
    return visitor_id


def commerce_session(request):
    if not request.store:
        return None
    session, _ = CommerceSession.objects.get_or_create(store=request.store,
        visitor_id=_visitor_id(request), defaults={"last_path": request.path,
            "user": request.user if request.user.is_authenticated else None})
    updates = {"last_path": request.path, "last_seen_at": timezone.now()}
    if request.user.is_authenticated and session.user_id != request.user.id:
        updates["user"] = request.user
    CommerceSession.objects.filter(id=session.id).update(**updates)
    for field, value in updates.items(): setattr(session, field, value)
    return session


def record_event(request, kind, *, product=None, metadata=None, always=False):
    if kind in ANALYTICS_EVENTS and not always and request.COOKIES.get("cookie_consent") != "all":
        return None
    session = commerce_session(request)
    if not session:
        return None
    JourneyEvent.objects.create(session=session, kind=kind, product=product,
        metadata=metadata or {})
    updates = {}
    if kind == "product_view": updates["product_view_count"] = F("product_view_count") + 1
    if kind == "checkout_started": updates["checkout_started_count"] = F("checkout_started_count") + 1
    if kind == "converted": updates["converted_at"] = timezone.now()
    if updates:
        CommerceSession.objects.filter(id=session.id).update(**updates)
    return session


def engagement_summary(store):
    now = timezone.now()
    period_start = now - timedelta(days=30)
    abandoned_cutoff = now - timedelta(minutes=30)
    sessions = CommerceSession.objects.filter(store=store, last_seen_at__gte=period_start)
    abandoned = sessions.filter(converted_at__isnull=True, cart_item_count__gt=0,
        last_seen_at__lte=abandoned_cutoff).select_related("user").order_by("-cart_value", "-last_seen_at")
    engaged = sessions.filter(converted_at__isnull=True, cart_item_count=0,
        product_view_count__gte=3).select_related("user").order_by("-product_view_count", "-last_seen_at")
    checkout_pending = sessions.filter(converted_at__isnull=True, checkout_started_count__gt=0)
    total, converted = sessions.count(), sessions.filter(converted_at__isnull=False).count()
    return {"abandoned_sessions": abandoned, "engaged_sessions": engaged,
        "abandoned_count": abandoned.count(),
        "abandoned_value": abandoned.aggregate(total=Sum("cart_value"))["total"] or 0,
        "engaged_count": engaged.count(), "checkout_pending_count": checkout_pending.count(),
        "conversion_rate": round(converted / total * 100, 1) if total else 0}


def send_abandoned_cart_recoveries(*, limit=50, minimum_idle_minutes=30):
    cutoff = timezone.now() - timedelta(minutes=minimum_idle_minutes)
    sessions = CommerceSession.objects.filter(converted_at__isnull=True, cart_item_count__gt=0,
        recovery_consent_at__isnull=False, recovery_sent_at__isnull=True,
        last_seen_at__lte=cutoff).exclude(recovery_email="").select_related("store").order_by("last_seen_at")[:limit]
    sent = 0
    for session in sessions:
        claimed_at = timezone.now()
        if not CommerceSession.objects.filter(id=session.id, recovery_sent_at__isnull=True).update(
                recovery_sent_at=claimed_at):
            continue
        recovery_url = (f"{settings.PUBLIC_BASE_URL}/cart/recover/{session.recovery_token}/"
            f"?store={session.store.slug}")
        body = render_to_string("registration/cart_recovery_email.txt", {"store": session.store,
            "cart_snapshot": session.cart_snapshot, "recovery_url": recovery_url})
        try:
            send_mail(f"Keranjang Anda di {session.store.name} masih tersimpan", body,
                settings.DEFAULT_FROM_EMAIL, [session.recovery_email], fail_silently=False)
        except Exception:
            CommerceSession.objects.filter(id=session.id, recovery_sent_at=claimed_at).update(
                recovery_sent_at=None)
            raise
        sent += 1
    return sent


def update_cart_snapshot(request, lines, subtotal):
    session = commerce_session(request)
    if not session:
        return
    snapshot = [{"key": line["key"], "product_id": line["product"].id, "name": line["product"].name,
        "sku": line["variant"].sku if line["variant"] else line["product"].sku,
        "quantity": line["quantity"], "unit_price": str(line["unit_price"])} for line in lines]
    CommerceSession.objects.filter(id=session.id).update(cart_snapshot=snapshot,
        cart_item_count=sum(line["quantity"] for line in lines), cart_value=subtotal,
        last_path=request.path, last_seen_at=timezone.now())
