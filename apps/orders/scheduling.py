"""Delivery dates, time slots and pre-order quotas. A shopper picks a date (and a slot when the
store has slots) at checkout whenever the store schedules deliveries or the cart holds a
pre-order product. Pre-order products are made to order: each delivery date has a quota per
product instead of warehouse stock."""
from datetime import date, timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from .models import DeliverySettings, DeliverySlot, OrderItem

# Orders that no longer take up a slot or a pre-order quota.
RELEASED_STATUSES = {"payment_failed", "cancelled", "refunded"}
# A cart line of a pre-order product without a daily quota may hold up to this many.
PREORDER_LINE_LIMIT = 99
DEFAULT_MAX_DAYS = 14


class ScheduleError(ValueError):
    pass


def delivery_settings(store):
    return DeliverySettings.objects.filter(store=store).first() or DeliverySettings(store=store)


def orderable_quantity(product, variant=None):
    """How many a shopper may put in the cart: stock for normal products, the daily quota (or a
    sane cap) for pre-order ones, which do not use warehouse stock."""
    if product.is_preorder:
        return product.preorder_daily_quota or PREORDER_LINE_LIMIT
    return variant.available_stock if variant else product.available_stock


def needs_schedule(store, lines, settings=None):
    settings = settings or delivery_settings(store)
    return settings.enabled or any(line["product"].is_preorder for line in lines)


def preorder_booked(product, day):
    return OrderItem.objects.filter(product=product, is_preorder=True,
        order__delivery_date=day).exclude(order__status__in=RELEASED_STATUSES).aggregate(
        total=Sum("quantity"))["total"] or 0


def preorder_remaining(product, day):
    """Units of a pre-order product still available for a delivery date; None if unlimited."""
    if product.preorder_daily_quota is None:
        return None
    return max(0, product.preorder_daily_quota - preorder_booked(product, day))


def _preorder_units(lines):
    units = {}
    for line in lines:
        if line["product"].is_preorder:
            product = line["product"]
            units[product.id] = (product, units.get(product.id, (product, 0))[1] + line["quantity"])
    return units.values()


def available_dates(store, lines, *, settings=None, today=None):
    """Dates a shopper may choose for this cart, earliest first."""
    settings = settings or delivery_settings(store)
    today = today or timezone.localdate()
    lead = max([settings.min_days if settings.enabled else 0, *(
        line["product"].preorder_days for line in lines if line["product"].is_preorder)])
    last = settings.max_days if settings.pk else DEFAULT_MAX_DAYS
    preorders = list(_preorder_units(lines))
    dates = []
    for offset in range(lead, max(lead, last) + 1):
        day = today + timedelta(days=offset)
        if day.weekday() in settings.closed_days:
            continue
        if any((remaining := preorder_remaining(product, day)) is not None and remaining < units
                for product, units in preorders):
            continue
        dates.append(day)
    return dates


def slot_options(store, day):
    """Active slots for a date with how many orders each can still take (None: unlimited)."""
    slots = list(DeliverySlot.objects.filter(store=store, is_active=True))
    if not slots or day is None:
        return [{"slot": slot, "remaining": slot.capacity, "full": False} for slot in slots]
    booked = dict(DeliverySlot.objects.filter(id__in=[slot.id for slot in slots],
        orders__delivery_date=day).exclude(orders__status__in=RELEASED_STATUSES).values_list(
        "id").annotate(total=Count("orders")))
    options = []
    for slot in slots:
        remaining = None if slot.capacity is None else max(0, slot.capacity - booked.get(slot.id, 0))
        options.append({"slot": slot, "remaining": remaining, "full": remaining == 0})
    return options


def validate_schedule(store, lines, day_text, slot_text, *, lock=False):
    """The (date, slot) a checkout may use, or ScheduleError. With lock=True (inside the order
    transaction) the slot row and pre-order products are locked so two shoppers cannot take the
    last place at the same time."""
    from apps.catalog.models import Product

    settings = delivery_settings(store)
    if not needs_schedule(store, lines, settings):
        return None, None
    try:
        day = date.fromisoformat(day_text or "")
    except ValueError as error:
        raise ScheduleError("Pilih tanggal pengiriman.") from error
    if lock:
        list(Product.objects.select_for_update().filter(
            id__in=[line["product"].id for line in lines if line["product"].is_preorder]))
    if day not in available_dates(store, lines, settings=settings):
        raise ScheduleError("Tanggal pengiriman ini tidak tersedia. Pilih tanggal lain.")
    slots = DeliverySlot.objects.filter(store=store, is_active=True)
    if not slots.exists():
        return day, None
    slot = (slots.select_for_update() if lock else slots).filter(
        id=int(slot_text) if str(slot_text or "").isdigit() else 0).first()
    if slot is None:
        raise ScheduleError("Pilih jam pengiriman.")
    option = next(option for option in slot_options(store, day) if option["slot"].id == slot.id)
    if option["full"]:
        raise ScheduleError(f"Jam {slot.label} pada tanggal itu sudah penuh. Pilih jam lain.")
    return day, slot
