from decimal import Decimal

from apps.catalog.models import Product, ProductVariant

from .sales import active_sale_offers, offer_remaining
from .scheduling import orderable_quantity
from .wholesale import product_quantities, tier_for, tier_unit_price


def get_cart(request):
    raw = request.session.get("cart", {})
    product_ids = []
    variant_ids = []
    for key in raw:
        kind, _, value = str(key).partition(":")
        if not value:  # Backwards compatibility with carts created before variant support.
            kind, value = "p", kind
        if not value.isdigit():
            continue
        if kind == "p":
            product_ids.append(value)
        elif kind == "v":
            variant_ids.append(value)
    products = {p.id: p for p in Product.objects.filter(
        id__in=product_ids, store=request.store, is_active=True,
    ).prefetch_related("inventory_levels", "wholesale_prices")}
    variants = {v.id: v for v in ProductVariant.objects.filter(
        id__in=variant_ids, product__store=request.store, product__is_active=True, is_active=True,
    ).select_related("product").prefetch_related("product__wholesale_prices")}
    offers = active_sale_offers(request.store, [*products, *(variant.product_id for variant in variants.values())])
    lines, subtotal = [], Decimal(0)
    for raw_key, raw_quantity in raw.items():
        key = str(raw_key)
        kind, _, value = key.partition(":")
        variant = variants.get(int(value)) if kind == "v" and value.isdigit() else None
        product_id = int(value or kind) if (value or kind).isdigit() else None
        product = variant.product if variant else products.get(product_id)
        if not product:
            continue
        regular_price = variant.effective_price if variant else product.price
        offer = offers.get(product.id)
        if offer and offer.sale_price >= regular_price:
            offer = None
        available = orderable_quantity(product, variant)
        campaign_remaining = offer_remaining(offer) if offer else None
        if campaign_remaining is not None:
            available = min(available, campaign_remaining)
        try:
            quantity = max(0, min(int(raw_quantity), available))
        except (TypeError, ValueError):
            quantity = 0
        if not quantity:
            continue
        unit_price = offer.sale_price if offer else regular_price
        lines.append({"key": key, "product": product, "variant": variant,
            "note": str(request.session.get("cart_notes", {}).get(key, ""))[:200],
            "unit_price": unit_price, "regular_price": regular_price, "available_stock": available,
            "sale_offer": offer, "wholesale": None, "quantity": quantity})
    apply_wholesale(lines)
    for line in lines:
        line["total"] = line["unit_price"] * line["quantity"]
        subtotal += line["total"]
    return lines, subtotal


def apply_wholesale(lines):
    """Harga grosir on lines that are not in a flash sale, by each product's total quantity."""
    quantities = product_quantities(lines)
    for line in lines:
        if line.get("sale_offer"):
            continue
        tier = tier_for(line["product"], quantities[line["product"].id])
        price = tier_unit_price(line["product"], line["variant"], tier)
        if tier and price < line["unit_price"]:
            line["unit_price"], line["wholesale"] = price, tier
