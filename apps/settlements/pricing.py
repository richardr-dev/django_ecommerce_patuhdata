"""What a mitra pays: its own price list first, then the general mitra price, then retail.

A mitra (for example AB Shop) is a ConsignmentPartner. Its prices live in PartnerProductPrice (the
"Harga mitra" editor on a product). Billing a mitra, in a settlement or on an invoice, must read
them from here, so a price is changed in one place."""
from .models import PartnerProductPrice


def partner_price(partner, product, variant=None):
    """The price for ``partner``, or None when the mitra has no special price (use retail)."""
    if partner is None or product is None:
        return None
    own = PartnerProductPrice.objects.filter(partner=partner, product=product).values_list(
        "price", flat=True).first()
    if own is not None:
        return own
    if variant is not None and variant.ab_shop_price is not None:
        return variant.ab_shop_price
    return product.ab_shop_price


def price_list(store, partner):
    """``{product id: price}`` for one mitra, for the invoice form."""
    prices = {}
    if partner is None:
        return prices
    for product_id, price in PartnerProductPrice.objects.filter(partner=partner,
            partner__store=store).values_list("product_id", "price"):
        prices[product_id] = price
    return prices
