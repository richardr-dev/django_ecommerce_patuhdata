"""Couriers' own tracking pages. Lakuin does not track parcels itself: the seller enters the
courier and tracking number, and the shopper checks it on the courier's site."""
import re
from pathlib import Path

from django.templatetags.static import static

# code: (name, tracking page). "{number}" is filled in where the page accepts it in the URL;
# otherwise the shopper pastes the number, which the button copies first.
COURIERS = {
    "jne": ("JNE", "https://www.jne.co.id/tracking-package"),
    "jnt": ("J&T Express", "https://jet.co.id/track"),
    "sicepat": ("SiCepat", "https://www.sicepat.com/"),
    "anteraja": ("AnterAja", "https://anteraja.id/tracking"),
    "ninja": ("Ninja Xpress", "https://www.ninjaxpress.co/id-id/tracking?id={number}"),
    "pos": ("Pos Indonesia", "https://www.posindonesia.co.id/id/tracking"),
    "tiki": ("TIKI", "https://www.tiki.id/id/track"),
    "lion": ("Lion Parcel", "https://lionparcel.com/track"),
    "ide": ("ID Express", "https://idexpress.com/lacak-paket/"),
    "spx": ("SPX Express", "https://spx.co.id/track"),
    "wahana": ("Wahana", "https://www.wahana.com/#lacakkiriman"),
}
ALIASES = {"jt": "jnt", "jet": "jnt", "jandt": "jnt", "jntexpress": "jnt", "ninjaxpress": "ninja",
    "posindonesia": "pos", "idexpress": "ide", "idx": "ide", "lionparcel": "lion",
    "shopeeexpress": "spx", "shopee": "spx"}


def courier_key(code):
    key = "".join(character for character in (code or "").lower() if character.isalnum())
    return ALIASES.get(key, key)


def courier_name(code):
    return COURIERS.get(courier_key(code), ((code or "").upper(), ""))[0]


def courier_tracking(order):
    """Name, number and tracking page for the order's parcel, or None without a number."""
    if not order.tracking_number:
        return None
    name, url = COURIERS.get(courier_key(order.courier_code), (courier_name(order.courier_code), ""))
    return {"name": name or order.shipping_service, "number": order.tracking_number,
        "url": url.replace("{number}", order.tracking_number)}


# Badge colours for the courier list at checkout (the courier's brand colour).
BRAND_COLOURS = {"jne": "#0a4ea2", "jnt": "#d6001c", "sicepat": "#c8102e", "anteraja": "#d81b7a",
    "ninja": "#c8102e", "pos": "#f37021", "tiki": "#1c3f94", "lion": "#e31b23", "ide": "#d50032",
    "spx": "#ee4d2d", "wahana": "#1f4e9e", "gosend": "#00aa13", "grab": "#00b14f"}
SHORT_NAMES = {"jnt": "J&T", "ninja": "Ninja", "pos": "POS", "ide": "IDexpress", "lion": "Lion",
    "spx": "SPX", "gosend": "GoSend", "grab": "Grab"}
# Text on the brand badge, short enough for its 52px box.
BADGE_LABELS = {"ide": "IDX", "anteraja": "Anter Aja", "sicepat": "SiCepat", "lion": "Lion",
    "gosend": "GoSend"}
# How rate providers spell a courier before the service name.
COURIER_PREFIXES = ("Ninja Express", "Ninja Xpress", "ID Express", "IDexpress", "Lion Parcel",
    "Pos Indonesia", "SiCepat", "AnterAja", "J&T Express", "J&T", "JNE", "TIKI", "SPX Express")
SERVICE_NAMES = {"reg": "Reguler", "yes": "YES (besok sampai)", "oke": "OKE (hemat)",
    "ez": "EZ", "eco": "Ekonomi", "ons": "ONS (besok sampai)", "sds": "Sameday"}
LOGO_DIR = Path(__file__).resolve().parents[2] / "static" / "img" / "couriers"


def courier_logo(key):
    """static/img/couriers/<key>.svg or .png when the store owner has added the courier's logo."""
    for extension in ("svg", "png", "webp"):
        if (LOGO_DIR / f"{key}.{extension}").exists():
            return static(f"img/couriers/{key}.{extension}")
    return ""


def _service_label(key, quote_name, service_code):
    """'Jalur Nugraha Ekakurir (JNE) REG' or 'JNE Reguler' → 'Reguler'."""
    name = re.sub(r"^.*?\)\s*", "", quote_name) if ")" in quote_name else quote_name
    known = COURIERS.get(key, ("", ""))[0]
    prefixes = {known, SHORT_NAMES.get(key, ""), key.upper(), "Pos", *COURIER_PREFIXES}
    for prefix in sorted(filter(None, prefixes), key=len, reverse=True):  # longest first
        if name.lower().startswith(prefix.lower() + " "):
            name = name[len(prefix) + 1:]
    name = name.strip() or service_code
    return SERVICE_NAMES.get(name.lower(), name)


def _eta(value):
    """'2 - 3 days', '2-3', '2 day', '1-2 HARI' → ('2–3 hari', 3): the label and the latest
    day it may arrive, which ranks the fastest service. Unknown → ('', None)."""
    numbers = [int(number) for number in re.findall(r"\d+", str(value or ""))][:2]
    if not numbers:
        return "", None
    if "jam" in str(value).lower() or "hour" in str(value).lower():
        return f"{'–'.join(map(str, numbers))} jam", 0
    low, high = min(numbers), max(numbers)
    return (f"{low} hari" if low == high else f"{low}–{high} hari"), high


def present_quotes(quotes):
    """Courier choices as a shopper reads them: brand badge, short name, Indonesian ETA,
    cheapest first, with the cheapest and the fastest tagged."""
    rows = []
    for quote in quotes:
        key = courier_key(quote["courier_code"])
        eta, days = _eta(quote["eta"])
        rows.append({**quote, "courier": key,
            "courier_name": SHORT_NAMES.get(key) or COURIERS.get(key, (key.upper(), ""))[0],
            "service": _service_label(key, quote["name"], quote["service_code"]),
            "eta_label": eta, "eta_days": days, "logo": courier_logo(key),
            "badge": BADGE_LABELS.get(key) or SHORT_NAMES.get(key) or key.upper()[:6],
            "colour": BRAND_COLOURS.get(key, "#475467"), "tags": []})
    rows.sort(key=lambda row: (row["price"], row["eta_days"] if row["eta_days"] is not None else 99))
    if rows:
        rows[0]["tags"].append("Termurah")
        timed = [row for row in rows if row["eta_days"] is not None]
        if timed:
            fastest = min(timed, key=lambda row: (row["eta_days"], row["price"]))
            if fastest is not rows[0] or len(rows) == 1:
                fastest["tags"].append("Tercepat")
    return rows
