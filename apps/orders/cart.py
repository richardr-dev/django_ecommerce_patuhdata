from decimal import Decimal

from apps.catalog.models import Product, ProductVariant


def get_cart(request):
    raw = request.session.get("cart", {})
    product_ids = []
    variant_ids = []
    for key in raw:
        kind, _, value = str(key).partition(":")
        if not value:  # Backwards compatibility with carts created before variant support.
            product_ids.append(kind)
        elif kind == "p":
            product_ids.append(value)
        elif kind == "v":
            variant_ids.append(value)
    products = {p.id: p for p in Product.objects.filter(
        id__in=product_ids, store=request.store, is_active=True,
    ).prefetch_related("inventory_levels")}
    variants = {v.id: v for v in ProductVariant.objects.filter(
        id__in=variant_ids, product__store=request.store, product__is_active=True, is_active=True,
    ).select_related("product")}
    lines, subtotal = [], Decimal(0)
    for raw_key, raw_quantity in raw.items():
        key = str(raw_key)
        kind, _, value = key.partition(":")
        variant = variants.get(int(value)) if kind == "v" and value.isdigit() else None
        product_id = int(value or kind) if (value or kind).isdigit() else None
        product = variant.product if variant else products.get(product_id)
        if not product:
            continue
        available = variant.stock if variant else product.available_stock
        try:
            quantity = max(0, min(int(raw_quantity), available))
        except (TypeError, ValueError):
            quantity = 0
        if not quantity:
            continue
        unit_price = variant.effective_price if variant else product.price
        total = unit_price * quantity
        lines.append({"key": key, "product": product, "variant": variant,
            "unit_price": unit_price, "available_stock": available,
            "quantity": quantity, "total": total})
        subtotal += total
    return lines, subtotal
