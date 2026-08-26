from dataclasses import dataclass
from urllib.parse import urlencode

from django.conf import settings

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


class RajaOngkirProvider:
    def __init__(self):
        if not settings.RAJAONGKIR_API_KEY or not settings.RAJAONGKIR_ORIGIN_ID:
            raise ProviderError("RajaOngkir API key and origin ID are not configured.")

    @property
    def headers(self):
        return {"key": settings.RAJAONGKIR_API_KEY}

    def search_areas(self, query):
        params = urlencode({"search": query, "limit": 10, "offset": 0})
        data = request_json(f"{settings.RAJAONGKIR_API_URL}/destination/domestic-destination?{params}",
            headers=self.headers)
        return [{"id": str(area["id"]), "name": area["label"], "postal_code": str(area.get("zip_code", "")),
            "province": area.get("province_name", ""), "city": area.get("city_name", ""),
            "district": area.get("district_name", ""), "village": area.get("subdistrict_name", "")}
            for area in data.get("data", []) if area.get("id")]

    def rates(self, destination_area_id, lines):
        weight = sum(max(1, _line_weight(line)) * line["quantity"] for line in lines)
        data = request_form_json(f"{settings.RAJAONGKIR_API_URL}/calculate/domestic-cost",
            {"origin": settings.RAJAONGKIR_ORIGIN_ID, "destination": destination_area_id,
                "weight": weight, "courier": settings.RAJAONGKIR_COURIERS, "price": "lowest"},
            headers=self.headers)
        quotes = [ShippingQuote(provider="rajaongkir", courier_code=rate.get("code", ""),
            service_code=rate.get("service", ""), name=f'{rate.get("name", "Kurir")} {rate.get("service", "")}'.strip(),
            price=int(rate.get("cost", 0)), eta=str(rate.get("etd", ""))) for rate in (data.get("data") or [])]
        if not quotes: raise ProviderError(data.get("meta", {}).get("message", "Tidak ada layanan RajaOngkir untuk tujuan ini."))
        return quotes
