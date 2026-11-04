"""Couriers' own tracking pages. Lakuin does not track parcels itself: the seller enters the
courier and tracking number, and the shopper checks it on the courier's site."""

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
