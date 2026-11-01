"""Marketplace connectors behind one interface, so the sync engine never knows which
marketplace it talks to. A live connector needs that marketplace's partner approval and
app credentials; until then a channel runs on CSV import."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


class ChannelNotReady(Exception):
    """The marketplace API cannot be used yet (no partner credentials or authorization)."""


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal
    name: str = ""


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
    placed_at: datetime | None = None
    courier: str = ""
    tracking_number: str = ""
    raw: dict = field(default_factory=dict)

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
    "tokopedia": Platform("Tokopedia", partner_program="TikTok Shop Partner Center (Tokopedia)"),
    "tiktok_shop": Platform("TikTok Shop", partner_program="TikTok Shop Partner Center"),
    "shopee": Platform("Shopee", partner_program="Shopee Open Platform"),
    "lazada": Platform("Lazada", partner_program="Lazada Open Platform"),
    "blibli": Platform("Blibli", partner_program="Blibli Seller API"),
}


def adapter_for(channel):
    return PLATFORMS[channel.platform].adapter(channel)
