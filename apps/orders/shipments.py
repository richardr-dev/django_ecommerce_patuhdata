import uuid

from django.db import transaction

from apps.integrations.http import ProviderError
from apps.integrations.shipping import (
    BITESHIP_TRACKING_STATUS,
    BiteshipShipmentProvider,
    shipping_origin,
)
from apps.shops.models import AuditLog

from .models import Order

# external_shipping_id holds this prefix while a booking request is in flight or its outcome is
# unknown; it stops a second click (or a retry) from booking a second courier pickup.
BOOKING_CLAIM_PREFIX = "booking:"


class ShipmentBookingError(ValueError):
    pass


def shipment_origin(store):
    origin = shipping_origin(store) or {"name": store.name, "phone": store.support_phone,
        "address": "", "postal_code": ""}
    missing = [label for label, key in (("nomor telepon toko", "phone"),
        ("alamat gudang", "address"), ("kode pos asal", "postal_code")) if not origin[key]]
    if missing:
        raise ShipmentBookingError("Lengkapi " + ", ".join(missing) + " sebelum memesan kurir.")
    return origin


def can_book_shipment(order):
    return (order.status == "packed" and order.shipping_provider == "biteship"
        and not order.external_shipping_id)


def is_booking_pending(order):
    return order.external_shipping_id.startswith(BOOKING_CLAIM_PREFIX)


def book_shipment(order, *, actor):
    """Book the courier the customer chose at checkout and store its waybill."""
    if order.shipping_provider != "biteship":
        raise ShipmentBookingError("Pesanan ini tidak memakai tarif Biteship; isi resi secara manual.")
    if not order.courier_code or not order.courier_service_code:
        raise ShipmentBookingError("Layanan kurir checkout tidak lengkap; isi resi secara manual.")
    origin = shipment_origin(order.store)
    provider = BiteshipShipmentProvider()
    claim = f"{BOOKING_CLAIM_PREFIX}{uuid.uuid4().hex}"
    claimed = Order.objects.filter(id=order.id, store=order.store, status="packed",
        external_shipping_id="").update(external_shipping_id=claim)
    if not claimed:
        raise ShipmentBookingError("Kurir hanya dapat dipesan sekali untuk pesanan berstatus Packed.")
    try:
        result = provider.create_order(order, origin)
    except ProviderError as error:
        if str(error).startswith("Provider returned HTTP 4"):
            # Biteship rejected the request, so nothing was booked and it is safe to retry.
            Order.objects.filter(id=order.id, external_shipping_id=claim).update(
                external_shipping_id="", tracking_error=str(error)[:300])
        else:
            Order.objects.filter(id=order.id, external_shipping_id=claim).update(tracking_error=(
                "Status booking tidak pasti. Cek dashboard Biteship sebelum memesan ulang; "
                "jika sudah terbooking, isi resinya secara manual.")[:300])
        raise
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(id=order.id)
        locked.external_shipping_id = result["id"]
        locked.tracking_number = result["waybill_id"] or locked.tracking_number
        locked.tracking_status = BITESHIP_TRACKING_STATUS.get(result["status"], "awaiting_pickup")
        locked.tracking_status_detail = "Kurir Biteship dipesan; menunggu pickup."
        locked.tracking_error = ""
        locked.tracking_last_checked_at = None
        locked.fulfillment_method = "merchant_pickup"
        locked.save(update_fields=["external_shipping_id", "tracking_number", "tracking_status",
            "tracking_status_detail", "tracking_error", "tracking_last_checked_at",
            "fulfillment_method"])
        AuditLog.objects.create(store=locked.store, actor=actor, action="order.shipment_booked",
            object_type="Order", object_id=str(locked.id), details={"provider": "biteship",
                "shipment_id": result["id"], "waybill": result["waybill_id"],
                "price": result["price"]})
    order.refresh_from_db()
    return result


def has_courier_label(order):
    """Biteship booked the courier, so the courier's own label can be printed."""
    return (order.shipping_provider == "biteship" and bool(order.external_shipping_id)
        and not is_booking_pending(order))


def courier_label_url(order):
    """Link to the official courier label PDF, cached for a day (Biteship keeps it)."""
    from django.core.cache import cache

    key = f"courier-label:{order.external_shipping_id}"
    url = cache.get(key)
    if not url:
        url = BiteshipShipmentProvider().shipping_label_url(order.external_shipping_id)
        cache.set(key, url, 86400)
    return url
