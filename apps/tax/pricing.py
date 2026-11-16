"""What a customer pays: their price list first, then retail less the list's discount, else retail;
and when they pay: the payment terms on the customer.

The invoice form prices lines in the browser from the same rules (``price_map`` feeds it), and the
server fills a missing due date from ``due_date_for``. Mitra prices are a separate list
(``settlements/pricing.py``): a mitra is a consignment partner, not a customer group."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from apps.catalog.models import PriceList, PriceListItem


def list_price(price_list, product, variant=None):
    """The price on ``price_list`` for this product (its variant's row first, then the product's),
    else retail less the list's discount; None when the list says nothing (sell at retail)."""
    if price_list is None or product is None:
        return None
    rows = PriceListItem.objects.filter(price_list=price_list, product=product)
    own = None
    if variant is not None:
        own = rows.filter(variant=variant).values_list("price", flat=True).first()
    if own is None:
        own = rows.filter(variant=None).values_list("price", flat=True).first()
    if own is not None:
        return own
    if price_list.discount_percent:
        base = variant.price if variant is not None and variant.price is not None else product.price
        return (Decimal(base) * (Decimal(100) - price_list.discount_percent) / Decimal(100)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)
    return None


def customer_price(customer, product, variant=None):
    return list_price(customer.price_list if customer is not None else None, product, variant)


def price_map(store):
    """``{product id: {price list id: "price"}}`` of the rows every active list holds, for the form."""
    prices = {}
    for product_id, list_id, price in PriceListItem.objects.filter(price_list__store=store,
            price_list__is_active=True, variant=None).values_list("product_id", "price_list_id", "price"):
        prices.setdefault(product_id, {})[str(list_id)] = f"{price:.0f}"
    return prices


def lists_for_form(store):
    """``{price list id: {"name", "discount"}}`` so the form can apply a list's discount too."""
    return {str(row.pk): {"name": row.name, "discount": f"{row.discount_percent:.2f}"}
        for row in PriceList.objects.filter(store=store, is_active=True)}


def terms_days(customer):
    return customer.payment_terms_days if customer is not None else None


def due_date_for(customer, issued_on):
    """The due date a customer's terms give, or None when they have none."""
    days = terms_days(customer)
    return issued_on + timedelta(days=days) if days is not None else None
