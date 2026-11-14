"""Marketplace connectors behind one interface, so the sync engine never knows which
marketplace it talks to. A live connector needs that marketplace's partner approval and
app credentials; until then a channel runs on CSV import."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

_TIME_FORMATS = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")


def parse_marketplace_time(value):
    """The time a marketplace file gives (an Excel date or text such as 15/12/2025 14:30:22),
    as an aware datetime in the store's time zone, or None when blank or unreadable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        found = value
    else:
        text = str(value).strip()
        found = None
        for layout in _TIME_FORMATS:
            try:
                found = datetime.strptime(text[:19] if "T" in text else text, layout)
                break
            except ValueError:
                continue
        if found is None:
            return None
    return timezone.make_aware(found) if timezone.is_naive(found) else found


class ChannelNotReady(Exception):
    """The marketplace API cannot be used yet (no partner credentials or authorization)."""


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal
    name: str = ""
    line_total: Decimal | None = None  # exact total when unit_price x quantity would round
    bundle: str = ""  # SKU of the bundle this line is a component of


# Where an order stands on the marketplace, whatever the marketplace calls it. Only ``active``,
# ``shipped`` and ``completed`` orders are sales; ``cancelled`` and ``returned`` take a sale back.
STATE_UNPAID, STATE_ACTIVE, STATE_SHIPPED = "unpaid", "active", "shipped"
STATE_COMPLETED, STATE_CANCELLED, STATE_RETURNED = "completed", "cancelled", "returned"
STATE_RANK = {STATE_UNPAID: 0, STATE_ACTIVE: 1, STATE_SHIPPED: 2, STATE_COMPLETED: 3}
_STATE_WORDS = [
    (STATE_UNPAID, ("belum bayar", "menunggu pembayaran", "unpaid", "pending payment")),
    (STATE_RETURNED, ("embali", "retur", "return", "refund")),
    (STATE_CANCELLED, ("batal", "cancel")),
    (STATE_COMPLETED, ("selesai", "berhasil", "sukses", "success", "complete", "delivered", "diterima", "terkirim")),
    (STATE_SHIPPED, ("kirim", "ship", "transit", "antar")),
]


def normalize_state(text):
    """Map a marketplace status label (Indonesian or English) to one of the STATE_* values.
    Unknown labels count as ``active`` so a new label never hides a sale."""
    label = (text or "").strip().lower()
    for state, words in _STATE_WORDS:
        if any(word in label for word in words):
            return state
    return STATE_ACTIVE


@dataclass(frozen=True)
class MarketplaceOrder:
    """A marketplace order normalized for import. Marketplaces collect the buyer's payment, so
    an imported order is already paid; ``total`` is what the buyer paid."""
    external_id: str
    buyer_name: str
    phone: str
    address: str
    lines: list[OrderLine]
    shipping_cost: Decimal = Decimal(0)
    discount: Decimal = Decimal(0)
    placed_at: datetime | None = None  # when the buyer ordered: the sale is dated by it
    paid_at: datetime | None = None  # when the marketplace received the payment
    courier: str = ""
    tracking_number: str = ""
    status_text: str = ""  # the marketplace's own label, kept for display
    raw: dict = field(default_factory=dict)

    @property
    def state(self):
        return normalize_state(self.status_text)

    @property
    def subtotal(self):
        return sum((line.unit_price * line.quantity for line in self.lines), Decimal(0))

    @property
    def total(self):
        return max(Decimal(0), self.subtotal - self.discount + self.shipping_cost)


class ChannelAdapter:
    """What a live marketplace connector implements."""

    def __init__(self, channel):
        self.channel = channel

    def fetch_orders(self, since):
        """Paid orders placed or updated after ``since``, as MarketplaceOrder objects."""
        raise ChannelNotReady(self.not_ready_message)

    def push_stock(self, listing, quantity):
        raise ChannelNotReady(self.not_ready_message)

    @property
    def not_ready_message(self):
        return (f"Integrasi API {PLATFORMS[self.channel.platform].label} menunggu persetujuan "
            "partner marketplace. Gunakan impor CSV sementara.")


@dataclass(frozen=True)
class Platform:
    label: str
    adapter: type = ChannelAdapter
    partner_program: str = ""


PLATFORMS = {
    "tokopedia": Platform("Tokopedia / TikTok Shop", partner_program="TikTok Shop Partner Center (Tokopedia)"),
    "tiktok_shop": Platform("TikTok Shop", partner_program="TikTok Shop Partner Center"),
    "shopee": Platform("Shopee", partner_program="Shopee Open Platform"),
    "lazada": Platform("Lazada", partner_program="Lazada Open Platform"),
    "blibli": Platform("Blibli", partner_program="Blibli Seller API"),
}


def adapter_for(channel):
    return PLATFORMS[channel.platform].adapter(channel)
