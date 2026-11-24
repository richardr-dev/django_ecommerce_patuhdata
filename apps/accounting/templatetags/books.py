"""Amounts in the books: {{ value|rp }} prints Rp1.250.000 or −Rp1.250.000 in the company's book currency
(apps/shops/currency.py; $1,250.00 once USD is switched on), {{ value|abs }}."""
from decimal import Decimal, InvalidOperation

from django import template

from apps.shops import currency

register = template.Library()


def _number(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@register.filter(name="abs")
def absolute(value):
    number = _number(value)
    return abs(number) if number is not None else value


@register.filter
def rp(value, blank_zero=""):
    """Rupiah with a real minus sign; ``|rp:"-"`` prints an en dash for zero."""
    number = _number(value)
    if number is None:
        return ""
    if not number and blank_zero:
        return "–"
    return currency.current().format(number)


@register.filter
def signed_rp(value):
    """+Rp150.000 for money in, −Rp10.000 for money out."""
    number = _number(value)
    if number is None:
        return ""
    return currency.current().format(number, signed=True)


@register.filter
def get(container, key):
    """{{ mapping|get:key }} for dictionaries whose key is a variable."""
    try:
        return container[key]
    except (KeyError, TypeError, IndexError):
        return ""
