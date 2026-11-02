from django import template

from apps.orders.barcodes import code128_svg

register = template.Library()


@register.filter
def code128(value):
    return code128_svg(str(value or ""))
