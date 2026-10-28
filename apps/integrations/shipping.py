from dataclasses import dataclass
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache

from .http import ProviderError, request_form_json, request_json


@dataclass(frozen=True)
class ShippingQuote:
    provider: str
    courier_code: str
    service_code: str
    name: str
    price: int
    eta: str


def _line_weight(line):
    variant = line.get("variant")
    return variant.weight_grams if variant and variant.weight_grams is not None else line["product"].weight_grams


class BiteshipProvider:
    def __init__(self):
        if not settings.BITESHIP_API_KEY or not settings.BITESHIP_ORIGIN_POSTAL_CODE:
            raise ProviderError("Biteship API key and origin postal code are not configured.")
        if settings.BITESHIP_API_KEY.startswith("biteship_live.") and not settings.BITESHIP_ALLOW_LIVE:
            raise ProviderError("Live Biteship access is disabled. Use a biteship_test token for demo quotes.")

    @property
    def headers(self):
        return {"Authorization": settings.BITESHIP_API_KEY}

    def search_areas(self, query):
        params = urlencode({"countries": "ID", "input": query, "type": "single"})
        data = request_json(f"{settings.BITESHIP_API_URL}/v1/maps/areas?{params}", headers=self.headers)
        return [{"id": area["id"], "name": area["name"],
            "postal_code": str(area.get("postal_code", "")),
            "province": area.get("administrative_division_level_1_name", ""),
            "city": area.get("administrative_division_level_2_name", ""),
            "district": area.get("administrative_division_level_3_name", ""),
            "village": area.get("administrative_division_level_4_name", "")}
            for area in data.get("areas", []) if area.get("id")]

    def rates(self, destination_postal_code, lines, destination_area_id=""):
        items = [{"name": line["product"].name, "description": line["product"].description[:255],
            "value": int(line["unit_price"]), "quantity": line["quantity"],
            "weight": max(1, _line_weight(line)), "length": line["product"].length_cm,
            "width": line["product"].width_cm, "height": line["product"].height_cm} for line in lines]
        destination = ({"destination_area_id": destination_area_id} if destination_area_id
            else {"destination_postal_code": int(destination_postal_code)})
        data = request_json(f"{settings.BITESHIP_API_URL}/v1/rates/couriers", method="POST",
            headers=self.headers, payload={"origin_postal_code": int(settings.BITESHIP_ORIGIN_POSTAL_CODE),
                "couriers": settings.BITESHIP_COURIERS, "items": items, **destination})
        quotes = []
        for rate in data.get("pricing", []):
            quotes.append(ShippingQuote(provider="biteship", courier_code=rate.get("courier_code", ""),
                service_code=rate.get("courier_service_code", ""), name=f'{rate.get("courier_name", "Kurir")} {rate.get("courier_service_name", "")}'.strip(),
                price=int(rate.get("price", 0)), eta=str(rate.get("duration", ""))))
        if not quotes:
            raise ProviderError("Tidak ada layanan pengiriman untuk tujuan ini.")
        return quotes


# Biteship order statuses mapped onto Order.TRACKING_STATUSES.
BITESHIP_TRACKING_STATUS = {
    "confirmed": "awaiting_pickup", "scheduled": "awaiting_pickup", "allocated": "awaiting_pickup",
    "picking_up": "awaiting_pickup", "picked": "in_transit", "dropping_off": "out_for_delivery",
    "delivered": "delivered", "return_in_transit": "returned", "returned": "returned",
    "rejected": "exception", "courier_not_found": "exception", "on_hold": "exception",
    "cancelled": "exception", "disposed": "exception",
}


def _biteship_booking_payload(order, origin):
    items = []
    for item in order.items.select_related("product", "variant"):
        weight = (item.variant.weight_grams if item.variant and item.variant.weight_grams is not None
            else item.product.weight_grams)
        items.append({"name": item.name[:100], "sku": item.sku, "value": int(item.unit_price),
            "quantity": item.quantity, "weight": max(1, weight), "length": item.product.length_cm,
            "width": item.product.width_cm, "height": item.product.height_cm})
    destination = {"destination_contact_name": order.customer_name,
        "destination_contact_phone": order.phone, "destination_address": order.shipping_address,
        "destination_note": order.delivery_notes}
    if order.destination_area_id:
        destination["destination_area_id"] = order.destination_area_id
    if order.postal_code.isdigit():
        destination["destination_postal_code"] = int(order.postal_code)
    if order.email:
        destination["destination_contact_email"] = order.email
    return {"shipper_contact_name": origin["name"], "shipper_contact_phone": origin["phone"],
        "shipper_organization": order.store.name[:100],
        "origin_contact_name": origin["name"], "origin_contact_phone": origin["phone"],
        "origin_address": origin["address"], "origin_postal_code": int(origin["postal_code"]),
        **destination, "courier_company": order.courier_code, "courier_type": order.courier_service_code,
        "delivery_type": "now", "order_note": f"Pesanan #{str(order.id)[:8]}",
        "reference_id": str(order.id), "metadata": {"order_id": str(order.id),
            "store_id": str(order.store_id)}, "items": items}


class BiteshipShipmentProvider(BiteshipProvider):
    """Books the courier chosen at checkout and reads its status back."""

    def create_order(self, order, origin):
        # Biteship has no idempotency key, so a timed-out booking must never be resent blindly.
        data = request_json(f"{settings.BITESHIP_API_URL}/v1/orders", method="POST",
            headers=self.headers, payload=_biteship_booking_payload(order, origin), attempts=1)
        if not data.get("id"):
            raise ProviderError(data.get("error") or "Biteship tidak mengembalikan ID pengiriman.")
        courier = data.get("courier") or {}
        return {"id": data["id"], "waybill_id": courier.get("waybill_id") or "",
            "tracking_id": courier.get("tracking_id") or "", "status": data.get("status", ""),
            "price": data.get("price"), "payload": data}

    def track_order(self, shipment_id):
        data = request_json(f"{settings.BITESHIP_API_URL}/v1/orders/{shipment_id}",
            headers=self.headers)
        courier = data.get("courier") or {}
        status = str(data.get("status") or "").lower()
        events = [{"status": str(item.get("status", "")),
            "description": str(item.get("note") or item.get("status") or "Pembaruan pengiriman"),
            "location": "", "occurred_at": str(item.get("updated_at") or ""), "payload": item}
            for item in (data.get("history") or courier.get("history") or [])]
        return {"status": status, "normalized": BITESHIP_TRACKING_STATUS.get(status, "unknown"),
            "detail": events[-1]["description"] if events else status,
            "waybill_id": courier.get("waybill_id") or "", "events": events, "payload": data}


class RajaOngkirProvider:
    @property
    def headers(self):
        if not settings.RAJAONGKIR_API_KEY:
            raise ProviderError("RajaOngkir API key is not configured.")
        return {"key": settings.RAJAONGKIR_API_KEY}

    def search_areas(self, query):
        params = urlencode({"search": query, "limit": 10, "offset": 0})
        data = request_json(f"{settings.RAJAONGKIR_API_URL}/destination/domestic-destination?{params}",
            headers=self.headers)
        return [{"id": str(area["id"]), "name": area["label"], "postal_code": str(area.get("zip_code", "")),
            "province": area.get("province_name", ""), "city": area.get("city_name", ""),
            "district": area.get("district_name", ""), "village": area.get("subdistrict_name", "")}
            for area in data.get("data", []) if area.get("id")]

    def resolve_postal_code(self, postal_code):
        postal_code = str(postal_code).strip()
        if not postal_code.isdigit() or len(postal_code) != 5:
            raise ProviderError("Kode pos RajaOngkir harus terdiri dari 5 digit.")
        cache_key = f"rajaongkir:postcode:{postal_code}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        exact = [area for area in self.search_areas(postal_code)
            if area["postal_code"] == postal_code]
        if not exact:
            raise ProviderError(
                f"Kode pos {postal_code} tidak ditemukan oleh RajaOngkir. Cari dan pilih alamat."
            )
        if len({area["id"] for area in exact}) > 1:
            raise ProviderError(
                f"Kode pos {postal_code} memiliki beberapa tujuan. Cari kecamatan lalu pilih alamat."
            )
        cache.set(cache_key, exact[0], timeout=86400)
        return exact[0]

    def origin_id(self):
        if settings.RAJAONGKIR_ORIGIN_ID:
            return settings.RAJAONGKIR_ORIGIN_ID
        return self.resolve_postal_code(settings.RAJAONGKIR_ORIGIN_POSTAL_CODE)["id"]

    def rates(self, destination_area_id, lines):
        if not (settings.RAJAONGKIR_ORIGIN_ID or settings.RAJAONGKIR_ORIGIN_POSTAL_CODE):
            raise ProviderError("RajaOngkir origin ID/postcode is not configured.")
        weight = sum(max(1, _line_weight(line)) * line["quantity"] for line in lines)
        data = request_form_json(f"{settings.RAJAONGKIR_API_URL}/calculate/domestic-cost",
            {"origin": self.origin_id(), "destination": destination_area_id,
                "weight": weight, "courier": settings.RAJAONGKIR_COURIERS, "price": "lowest"},
            headers=self.headers)
        quotes = [ShippingQuote(provider="rajaongkir", courier_code=rate.get("code", ""),
            service_code=rate.get("service", ""), name=f'{rate.get("name", "Kurir")} {rate.get("service", "")}'.strip(),
            price=int(rate.get("cost", 0)), eta=str(rate.get("etd", ""))) for rate in (data.get("data") or [])]
        if not quotes: raise ProviderError(data.get("meta", {}).get("message", "Tidak ada layanan RajaOngkir untuk tujuan ini."))
        return quotes

    def track(self, airway_bill, courier_code, recipient_phone=""):
        params = {"awb": airway_bill.strip(), "courier": courier_code.strip().lower()}
        phone_digits = "".join(character for character in recipient_phone if character.isdigit())
        if phone_digits:
            params["last_phone_number"] = phone_digits[-5:]
        data = request_form_json(
            f"{settings.RAJAONGKIR_API_URL}/track/waybill?{urlencode(params)}", {},
            headers=self.headers)
        tracking = data.get("data") or (data.get("rajaongkir") or {}).get("result") or {}
        summary = tracking.get("summary") or {}
        delivery = tracking.get("delivery_status") or {}
        raw_history = (tracking.get("manifest") or tracking.get("history") or
            tracking.get("tracking_history") or [])
        events = []
        for item in raw_history:
            occurred_at = str(item.get("manifest_date") or item.get("date") or
                item.get("time") or item.get("updated_at") or "")
            extra_time = str(item.get("manifest_time") or "")
            if extra_time and extra_time not in occurred_at:
                occurred_at = f"{occurred_at} {extra_time}".strip()
            events.append({
                "status": str(item.get("manifest_code") or item.get("status") or
                    item.get("code") or ""),
                "description": str(item.get("manifest_description") or
                    item.get("description") or item.get("status") or "Pembaruan pengiriman"),
                "location": str(item.get("city_name") or item.get("location") or ""),
                "occurred_at": occurred_at,
                "payload": item,
            })
        latest_status = str(delivery.get("status") or tracking.get("last_status") or
            summary.get("status") or (events[-1]["description"] if events else "UNKNOWN"))
        detail = str(delivery.get("pod_receiver") or delivery.get("description") or
            (events[-1]["description"] if events else latest_status))
        return {"status": latest_status, "detail": detail, "events": events, "payload": tracking}
