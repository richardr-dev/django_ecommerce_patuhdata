"""Small text helpers for the order screens."""
import re

from django import template

register = template.Library()


@register.filter
def compact_mask(value):
    """Marketplaces hide a buyer's street with rows of asterisks; show one short mark for each run."""
    return re.sub(r"\*{3,}", "•••", str(value or ""))
