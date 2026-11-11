"""CRM sync: link Order records to Customer profiles."""
import logging
import re

logger = logging.getLogger(__name__)

UNSOLD = {"pending", "payment_failed", "cancelled"}
MARKETPLACE_CHANNELS = {"shopee", "tokopedia", "tiktok_shop", "lazada"}

_PHONE_RE = re.compile(r"^\+?[\d\s\-]{6,15}$")


def _looks_like_phone(value: str) -> bool:
    return bool(_PHONE_RE.match(value.strip()))


def sync_customer_from_order(order):
    """Find or create a Customer for this order and link the FK. Idempotent."""
    from apps.shops.models import Customer

    if order.customer_id:
        return order.customer

    store = order.store
    customer = None

    # 1. For marketplace orders: resolve via ChannelBuyer FIRST.
    #    Marketplace orders store buyer usernames (not phone numbers) in the
    #    phone field, so we must not use phone matching here.
    if order.channel in MARKETPLACE_CHANNELS:
        try:
            co = order.channel_order
            if co.buyer_id:
                cb = co.buyer
                try:
                    customer = cb.customer  # reverse OneToOneField
                except Exception:
                    customer = Customer.objects.create(
                        store=store,
                        name=cb.name or order.customer_name or "Pembeli Marketplace",
                        phone=cb.phone or "",
                        channel_buyer=cb,
                        status=Customer.STATUS_ACTIVE,
                    )
        except Exception:
            pass

    # 2. Phone match for non-marketplace channels (web, manual, POS, etc.)
    if not customer and order.channel not in MARKETPLACE_CHANNELS:
        phone = (order.phone or "").strip()
        if phone and _looks_like_phone(phone):
            customer = Customer.objects.filter(store=store, phone=phone).first()
            if not customer:
                customer = Customer.objects.create(
                    store=store,
                    name=order.customer_name or phone,
                    phone=phone,
                    status=Customer.STATUS_ACTIVE,
                )

    # 3. Name-only fallback (non-marketplace, no usable phone)
    if not customer and order.customer_name and order.channel not in MARKETPLACE_CHANNELS:
        customer = Customer.objects.create(
            store=store,
            name=order.customer_name,
            status=Customer.STATUS_ACTIVE,
        )

    if customer:
        from apps.orders.models import Order as OrderModel
        OrderModel.objects.filter(pk=order.pk).update(customer=customer)
        order.customer = customer

    return customer


def backfill_customers(store, limit=2000):
    """Link all unlinked orders for a store. Returns count linked."""
    from apps.orders.models import Order
    unlinked = list(
        Order.objects.filter(store=store, customer__isnull=True)
        .select_related("store")[:limit]
    )
    count = 0
    for order in unlinked:
        try:
            if sync_customer_from_order(order):
                count += 1
        except Exception:
            logger.exception("crm sync failed for order %s", order.pk)
    return count
