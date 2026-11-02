import csv
import io
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, ProductVariant
from apps.integrations.http import ProviderError
from apps.integrations.reconciliation import confirm_payment
from apps.orders.inventory import reserve_from_levels, reserve_variant
from apps.orders.models import Order, OrderItem, OrderStatusEvent

from .adapters import PLATFORMS, ChannelNotReady, MarketplaceOrder, OrderLine, adapter_for
from .models import ChannelListing, ChannelOrder, SalesChannel

CSV_COLUMNS = ["order_id", "buyer_name", "phone", "address", "sku", "quantity", "unit_price",
    "shipping_cost", "discount", "courier", "tracking_number"]


class ChannelImportError(ValueError):
    pass


def _resolve_sku(store, sku):
    variant = ProductVariant.objects.select_related("product").filter(product__store=store,
        sku=sku, is_active=True).first()
    if variant:
        return variant.product, variant
    product = Product.objects.filter(store=store, sku=sku).first()
    if not product:
        raise ChannelImportError(f"SKU {sku} tidak ada di katalog Lakuin.")
    if product.variants.filter(is_active=True).exists():
        raise ChannelImportError(f"SKU {sku} punya varian: gunakan SKU varian di marketplace.")
    return product, None


def import_order(channel, data: MarketplaceOrder):
    """Create the paid Lakuin order for a marketplace order, reserving its stock. Returns
    (order, created). All or nothing: when stock is short nothing is saved, so the seller can
    restock and import again."""
    existing = ChannelOrder.objects.filter(channel=channel,
        external_order_id=data.external_id).select_related("order").first()
    if existing:
        return existing.order, False
    if not data.lines:
        raise ChannelImportError("Pesanan tanpa produk.")
    store = channel.store
    resolved = [(line, *_resolve_sku(store, line.sku)) for line in data.lines]
    label = PLATFORMS[channel.platform].label
    try:
        with transaction.atomic():
            subtotal = data.subtotal
            order = Order.objects.create(store=store,
                customer_name=(data.buyer_name or f"Pembeli {label}")[:120],
                phone=data.phone[:30] or "-", shipping_address=data.address or "-",
                subtotal=subtotal, discount=min(data.discount, subtotal),
                shipping_cost=data.shipping_cost, total=data.total,
                payment_provider=channel.platform, payment_method="marketplace",
                payment_reference=f"{channel.platform}-{data.external_id}"[:160],
                paid_to_store_account=True, shipping_service=data.courier[:80],
                tracking_number=data.tracking_number[:100],
                idempotency_key=f"ch{channel.id}-{data.external_id}"[:64])
            OrderStatusEvent.objects.create(order=order, status="pending",
                note=f"Diimpor dari {label} · {channel.name}")
            for line, product, variant in resolved:
                item = OrderItem.objects.create(order=order, product=product, variant=variant,
                    name=(line.name or product.name)[:160], sku=line.sku,
                    variant_name=variant.name if variant else "", unit_price=line.unit_price,
                    quantity=line.quantity, line_total=line.unit_price * line.quantity)
                _reserve(item, product, variant, line.quantity)
            confirm_payment(order, provider=channel.platform, external_id=data.external_id,
                amount=order.total, currency="IDR", payload=data.raw,
                note=f"Dibayar melalui {label}")
            ChannelOrder.objects.create(channel=channel, order=order,
                external_order_id=data.external_id)
    except IntegrityError:  # imported concurrently
        existing = ChannelOrder.objects.filter(channel=channel,
            external_order_id=data.external_id).select_related("order").first()
        if existing:
            return existing.order, False
        raise
    return order, True


def _reserve(item, product, variant, quantity):
    if variant:
        variant = ProductVariant.objects.select_for_update().get(id=variant.id)
        if variant.available_stock < quantity:
            raise ChannelImportError(_shortage(item.sku, quantity, variant.available_stock))
        reserve_variant(item, variant, quantity)
        return
    levels = list(InventoryLevel.objects.select_for_update().filter(product=product,
        warehouse__store=product.store, warehouse__is_active=True)
        .order_by("warehouse__priority", "warehouse_id"))
    available = sum(level.available for level in levels)
    if available < quantity:
        raise ChannelImportError(_shortage(item.sku, quantity, available))
    reserve_from_levels(item, levels, quantity, reason="marketplace_order")


def _shortage(sku, wanted, available):
    return (f"Stok SKU {sku} kurang: marketplace menjual {wanted}, tersedia {available}. "
        "Tambah stok lalu impor ulang.")


def parse_amount(text):
    """Rupiah from a spreadsheet cell: "15000", "15.000", "Rp 15.000" or "15000,50"."""
    text = re.sub(r"(?i)rp|\s", "", str(text or ""))
    if not text:
        return Decimal(0)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", text):
        text = text.replace(".", "")
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation as error:
        raise ChannelImportError(f"Nominal tidak valid: {text}") from error


def parse_orders_csv(upload):
    """Group the Lakuin order template (one row per product line) into MarketplaceOrders."""
    text = upload.read().decode("utf-8-sig") if hasattr(upload, "read") else upload
    rows = csv.DictReader(io.StringIO(text))
    rows.fieldnames = [name.strip().lower() for name in rows.fieldnames or []]
    missing = {"order_id", "sku", "quantity", "unit_price"} - set(rows.fieldnames)
    if missing:
        raise ChannelImportError(f"Kolom wajib tidak ada: {', '.join(sorted(missing))}")
    grouped = OrderedDict()
    for number, row in enumerate(rows, start=2):
        order_id = (row.get("order_id") or "").strip()
        if not order_id:
            continue
        try:
            quantity = int(parse_amount(row.get("quantity")))
            if quantity <= 0:
                raise ChannelImportError("jumlah harus lebih dari 0")
            line = OrderLine(sku=(row.get("sku") or "").strip(), quantity=quantity,
                unit_price=parse_amount(row.get("unit_price")))
        except ChannelImportError as error:
            raise ChannelImportError(f"Baris {number}: {error}") from error
        head = grouped.setdefault(order_id, {"row": row, "lines": []})
        head["lines"].append(line)
    return [MarketplaceOrder(external_id=order_id[:80],
        buyer_name=(head["row"].get("buyer_name") or "").strip(),
        phone=(head["row"].get("phone") or "").strip(),
        address=(head["row"].get("address") or "").strip(), lines=head["lines"],
        shipping_cost=parse_amount(head["row"].get("shipping_cost")),
        discount=parse_amount(head["row"].get("discount")),
        courier=(head["row"].get("courier") or "").strip(),
        tracking_number=(head["row"].get("tracking_number") or "").strip(), raw=dict(head["row"]))
        for order_id, head in grouped.items()]


def import_orders(channel, orders):
    """Import each order on its own, so one bad order does not block the rest."""
    summary = {"created": 0, "duplicates": 0, "errors": []}
    for data in orders:
        try:
            _, created = import_order(channel, data)
        except ChannelImportError as error:
            summary["errors"].append(f"{data.external_id}: {error}")
            continue
        summary["created" if created else "duplicates"] += 1
    return summary


def match_listings(channel):
    """List every active SKU of the store on this channel. Returns how many were added."""
    store, existing = channel.store, set(channel.listings.values_list("sku", flat=True))
    new = [ChannelListing(channel=channel, product=variant.product, variant=variant, sku=variant.sku)
        for variant in ProductVariant.objects.filter(product__store=store, is_active=True,
            product__is_active=True).exclude(sku="").select_related("product")
        if variant.sku not in existing]
    new += [ChannelListing(channel=channel, product=product, sku=product.sku)
        for product in Product.objects.filter(store=store, is_active=True).exclude(sku="")
            .exclude(variants__is_active=True) if product.sku not in existing]
    ChannelListing.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def push_stock_updates(limit=200):
    """Tell API-connected channels about stock that changed since it was last pushed."""
    pushed = 0
    listings = ChannelListing.objects.filter(channel__is_active=True,
        channel__connection="api").select_related("channel", "product", "variant").order_by(
        F("last_pushed_at").asc(nulls_first=True))[:limit]
    for listing in listings:
        quantity = listing.available_stock
        if quantity == listing.last_pushed_stock:
            continue
        try:
            adapter_for(listing.channel).push_stock(listing, quantity)
        except (ChannelNotReady, ProviderError) as error:
            listing.sync_error = str(error)[:300]
            listing.save(update_fields=["sync_error"])
            continue
        listing.last_pushed_stock, listing.last_pushed_at = quantity, timezone.now()
        listing.sync_error = ""
        listing.save(update_fields=["last_pushed_stock", "last_pushed_at", "sync_error"])
        pushed += 1
    return pushed


def pull_channel_orders():
    """Import new paid orders from API-connected channels. The cursor advances only when the
    whole batch imported, so a short-stock order is retried after the seller restocks."""
    imported = 0
    for channel in SalesChannel.objects.filter(is_active=True, connection="api"):
        started = timezone.now()
        try:
            orders = adapter_for(channel).fetch_orders(since=channel.last_order_sync_at)
        except (ChannelNotReady, ProviderError) as error:
            channel.last_error = str(error)[:300]
            channel.save(update_fields=["last_error"])
            continue
        summary = import_orders(channel, orders)
        imported += summary["created"]
        channel.last_error = "; ".join(summary["errors"])[:300]
        if not summary["errors"]:
            channel.last_order_sync_at = started
        channel.save(update_fields=["last_error", "last_order_sync_at"])
    return imported
