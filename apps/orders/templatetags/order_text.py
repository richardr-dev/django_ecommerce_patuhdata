"""Small text helpers for the order screens."""
import re

from django import template

register = template.Library()


@register.filter
def compact_mask(value):
    """Marketplaces hide a buyer's street with rows of asterisks; show one short mark for each run."""
    return re.sub(r"\*{3,}", "•••", str(value or ""))


ARCHIVE_SUFFIX = "~dibersihkan"


@register.filter
def order_ref(order):
    """The number people recognise: the marketplace's order number (without the suffix an old clean-up
    appended to superseded copies), else the short #id."""
    ref = (getattr(order, "external_order_id", "") or "").replace(ARCHIVE_SUFFIX, "").strip()
    if ref and getattr(order, "is_marketplace", False):
        return ref
    return "#" + str(order.id)[:8]


@register.filter
def order_copy(order):
    """What the copy button puts on the clipboard: the number without the leading #."""
    return order_ref(order).lstrip("#")


@register.filter
def is_archived(order):
    return (getattr(order, "external_order_id", "") or "").endswith(ARCHIVE_SUFFIX)
