import hashlib
import json
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

from apps.integrations.http import ProviderError
from apps.integrations.shipping import RajaOngkirProvider

from .models import Order, ShipmentTrackingEvent
from .services import transition_order


def normalize_tracking_status(value):
    status = str(value or "").upper()
    if any(term in status for term in ("RETURN", "RETUR")):
        return "returned"
    if any(term in status for term in ("DELIVERED", "DITERIMA", "SUCCESS", "POD")):
        return "delivered"
    if any(term in status for term in ("OUT FOR DELIVERY", "WITH DELIVERY COURIER",
            "SEDANG DIANTAR", "ANTAR KE PENERIMA")):
        return "out_for_delivery"
    if any(term in status for term in ("FAILED", "UNDELIVERED", "EXCEPTION", "PROBLEM",
            "GAGAL", "RUSAK", "HILANG")):
        return "exception"
    if any(term in status for term in ("TRANSIT", "MANIFEST", "ON PROCESS", "PICKED",
            "RECEIVED BY COURIER", "DEPARTED", "SORTING", "GATEWAY")):
        return "in_transit"
    if any(term in status for term in ("PICKUP", "BOOKED", "WAITING", "AWAITING")):
        return "awaiting_pickup"
    return "unknown"


def sync_order_tracking(order):
    if not order.tracking_number or not order.courier_code:
        raise ProviderError("Nomor resi dan kode kurir wajib diisi sebelum sinkronisasi.")
    result = RajaOngkirProvider().track(order.tracking_number, order.courier_code, order.phone)
    normalized = normalize_tracking_status(result["status"])
    now = timezone.now()
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(id=order.id)
        for event in result["events"]:
            encoded = json.dumps(event["payload"], sort_keys=True, ensure_ascii=False,
                default=str).encode()
            fingerprint = hashlib.sha256(encoded).hexdigest()
            ShipmentTrackingEvent.objects.get_or_create(order=locked, fingerprint=fingerprint,
                defaults={"provider": "rajaongkir", "status": event["status"][:80],
                    "description": event["description"][:500], "location": event["location"][:160],
                    "occurred_at": event["occurred_at"][:80], "payload": event["payload"]})
        locked.tracking_status = normalized
        locked.tracking_status_detail = result["detail"][:300]
        locked.tracking_last_checked_at = now
        locked.tracking_error = ""
        locked.save(update_fields=["tracking_status", "tracking_status_detail",
            "tracking_last_checked_at", "tracking_error"])
        if normalized in {"in_transit", "out_for_delivery"} and locked.status == "packed":
            transition_order(locked, "shipped", note="Status kurir tersinkron otomatis")
        elif normalized == "delivered" and locked.status == "shipped":
            transition_order(locked, "delivered", note="Terkirim berdasarkan tracking kurir")
    order.refresh_from_db()
    return result


def sync_active_shipments(*, limit=25, minimum_interval_minutes=15):
    cutoff = timezone.now() - timedelta(minutes=minimum_interval_minutes)
    orders = (Order.objects.filter(status__in={"packed", "shipped"})
        .exclude(tracking_number="").exclude(courier_code="")
        .exclude(tracking_status__in={"delivered", "returned"})
        .filter(models.Q(tracking_last_checked_at__isnull=True) |
            models.Q(tracking_last_checked_at__lte=cutoff))
        .order_by("tracking_last_checked_at", "created_at")[:limit])
    synced = 0
    for order in orders:
        try:
            sync_order_tracking(order)
        except ProviderError as error:
            Order.objects.filter(id=order.id).update(tracking_last_checked_at=timezone.now(),
                tracking_error=str(error)[:300])
        else:
            synced += 1
    return synced
