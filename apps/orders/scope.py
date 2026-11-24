"""Which orders belong on the Pesanan page.

Pesanan is the website's fulfilment queue: check payment, pack, ship. Orders imported from a
marketplace file (Shopee, Tokopedia, TikTok Shop, Lazada) are already handled by the marketplace
and live in Invoice penjualan, the channel recap and the books. Listing them in both places
showed the same sale twice, so Pesanan hides them unless asked."""
from .models import Order

MARKETPLACE_CHANNELS = {"shopee", "tokopedia", "tiktok_shop", "lazada"}


def marketplace_orders(store):
    """Imported from a marketplace file, or sold on a marketplace channel."""
    from django.db.models import Q
    return Order.objects.filter(store=store).filter(
        Q(channel__in=MARKETPLACE_CHANNELS) | Q(channel_order__isnull=False))


def website_orders(store):
    """Everything made in the system: online shop, manual, POS, TV shopping, reseller."""
    return Order.objects.filter(store=store).exclude(order_type="opening").exclude(
        pk__in=marketplace_orders(store).values("pk"))
