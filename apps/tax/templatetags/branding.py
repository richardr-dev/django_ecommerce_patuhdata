from django import template

from apps.tax import branding

register = template.Library()


@register.simple_tag
def brand(store, invoice=None):
    """{% brand merchant_store invoice as b %} gives b.logo_url, b.accent, b.footer, b.name …"""
    return branding.brand(store, invoice)
