"""Harga grosir: quantity price tiers a seller sets per product (e.g. 10+ pcs Rp45.000).

The tier is picked from the total quantity of the product in one order, all variants counted
together. A variant with its own price drops by the same percentage as the product price, so
an XL that costs more than an S stays more expensive at every tier."""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

MAX_TIERS = 5


def tiers(product):
    """The product's tiers, lowest quantity first (uses a prefetch when there is one)."""
    return sorted(product.wholesale_prices.all(), key=lambda tier: tier.min_quantity)


def tier_for(product, quantity):
    """The tier that applies to ``quantity`` units of ``product``, or None."""
    chosen = None
    for tier in tiers(product):
        if quantity >= tier.min_quantity:
            chosen = tier
    return chosen


def tier_unit_price(product, variant, tier):
    """The unit price of this product/variant at ``tier`` (never above its regular price)."""
    regular = variant.effective_price if variant else product.price
    if tier is None:
        return regular
    if variant is None or variant.price is None or variant.price == product.price or not product.price:
        return min(regular, tier.price)
    scaled = (regular * tier.price / product.price).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return min(regular, scaled)


def product_quantities(lines):
    """Total quantity per product id over cart/order lines."""
    totals = defaultdict(int)
    for line in lines:
        totals[line["product"].id] += line["quantity"]
    return totals
