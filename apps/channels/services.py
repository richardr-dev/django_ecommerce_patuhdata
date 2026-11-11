import csv
import io
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import InventoryLevel, Product, ProductVariant
from apps.channels.adapters import OrderLine
from apps.integrations.http import ProviderError
from apps.integrations.reconciliation import confirm_payment
from apps.orders.inventory import reserve_from_levels, reserve_variant
from apps.orders.models import Order, OrderItem, OrderStatusEvent

from .adapters import PLATFORMS, ChannelNotReady, MarketplaceOrder, OrderLine, adapter_for
from .models import ChannelBuyer, ChannelListing, ChannelOrder, ImportBatch, SalesChannel

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


def import_order(channel, data: MarketplaceOrder, soft: bool = False):
    """Create the paid Lakuin order for a marketplace order. Returns (order, created).

    soft=True: historical import mode — auto-creates placeholder products for unrecognised
    SKUs and skips stock deduction. Use for importing past orders when catalog isn't set up yet.
    soft=False (default): strict mode — raises ChannelImportError on unknown SKU or low stock.
    """
    existing = ChannelOrder.objects.filter(channel=channel,
        external_order_id=data.external_id).select_related("order").first()
    if existing:
        return existing.order, False
    if not data.lines:
        raise ChannelImportError("Pesanan tanpa produk.")
    store = channel.store
    label = PLATFORMS[channel.platform].label

    if soft:
        resolved = [(line, *_resolve_sku_soft(store, line)) for line in data.lines]
    else:
        resolved = [(line, *_resolve_sku(store, line.sku)) for line in data.lines]

    try:
        with transaction.atomic():
            subtotal = data.subtotal
            notes = "Stok belum dikurangi — perlu review SKU produk." if soft else ""
            order = Order.objects.create(store=store,
                channel=channel.platform,
                external_order_id=data.external_id[:100],
                customer_name=(data.buyer_name or f"Pembeli {label}")[:120],
                phone=data.phone[:30] or "-", shipping_address=data.address or "-",
                subtotal=subtotal, discount=min(data.discount, subtotal),
                shipping_cost=data.shipping_cost, total=data.total,
                payment_provider=channel.platform, payment_method="marketplace",
                payment_reference=f"{channel.platform}-{data.external_id}"[:160],
                paid_to_store_account=True, shipping_service=data.courier[:80],
                tracking_number=data.tracking_number[:100],
                delivery_notes=notes[:300],
                idempotency_key=f"ch{channel.id}-{data.external_id}"[:64])
            event_note = f"Diimpor dari {label} · {channel.name}"
            if soft:
                event_note += " (mode historis — stok belum dikurangi)"
            OrderStatusEvent.objects.create(order=order, status="pending", note=event_note)
            for line, product, variant in resolved:
                item = OrderItem.objects.create(order=order, product=product, variant=variant,
                    name=(line.name or product.name)[:160], sku=line.sku,
                    variant_name=variant.name if variant else "", unit_price=line.unit_price,
                    quantity=line.quantity, line_total=line.unit_price * line.quantity)
                if not soft:
                    _reserve(item, product, variant, line.quantity)
            confirm_payment(order, provider=channel.platform, external_id=data.external_id,
                amount=order.total, currency="IDR", payload=data.raw,
                note=f"Dibayar melalui {label}")
            ChannelOrder.objects.create(channel=channel, order=order,
                external_order_id=data.external_id,
                batch=_current_batch.get(channel.id))
            _upsert_buyer(channel, data, order)
    except IntegrityError:  # imported concurrently
        existing = ChannelOrder.objects.filter(channel=channel,
            external_order_id=data.external_id).select_related("order").first()
        if existing:
            return existing.order, False
        raise
    return order, True


def _resolve_sku_soft(store, line: "OrderLine"):
    """Resolve SKU softly: return existing product/variant or create a placeholder product.
    Never raises — always returns something so historical imports always succeed."""
    # Try strict match first
    try:
        return _resolve_sku(store, line.sku)
    except ChannelImportError:
        pass

    # Auto-create a placeholder product so the OrderItem FK constraint is satisfied
    sku = (line.sku or "").strip()
    base_name = (line.name or sku or "Produk Impor")[:140]
    # Slug: use SKU (guaranteed unique per store) or fallback to slugify(name)
    candidate_slug = slugify(sku) if sku else slugify(base_name)[:50]
    # Ensure slug uniqueness by appending a suffix when collisions exist
    slug = candidate_slug
    suffix = 1
    while Product.objects.filter(store=store, slug=slug).exists():
        slug = f"{candidate_slug}-{suffix}"
        suffix += 1

    name = f"{base_name} [{sku}]" if sku else base_name[:160]

    if sku:
        product, _ = Product.objects.get_or_create(
            store=store,
            sku=sku,
            defaults={
                "name": name,
                "slug": slug,
                "is_active": True,
                "price": line.unit_price,
                "description": "Dibuat otomatis dari impor marketplace. Perlu review SKU.",
            },
        )
    else:
        product, _ = Product.objects.get_or_create(
            store=store,
            name=name,
            defaults={
                "slug": slug,
                "is_active": True,
                "price": line.unit_price,
                "description": "Dibuat otomatis dari impor marketplace. Perlu review SKU.",
            },
        )
    return product, None


def _upsert_buyer(channel: "SalesChannel", data: MarketplaceOrder, order):
    """Create or update the ChannelBuyer record for this marketplace order."""
    raw = data.raw or {}
    external_id = (raw.get("shopee_username") or raw.get("tokopedia_username")
                   or data.phone or data.external_id)
    if not external_id or external_id == "-":
        return
    city = raw.get("city", "")
    province = raw.get("province", "")
    name = data.buyer_name or ""
    # Strip masked-name marker (@username) to store clean name
    if name.startswith("@"):
        name = ""
    buyer, created = ChannelBuyer.objects.get_or_create(
        store=channel.store,
        platform=channel.platform,
        external_id=external_id[:160],
        defaults={"name": name[:160], "phone": data.phone[:40],
                  "city": city[:100], "province": province[:100],
                  "last_address": data.address or ""},
    )
    if not created:
        # Update with latest info (Shopee may unmask more data later)
        update_fields = []
        if name and not buyer.name:
            buyer.name = name[:160]; update_fields.append("name")
        if city and not buyer.city:
            buyer.city = city[:100]; update_fields.append("city")
        if province and not buyer.province:
            buyer.province = province[:100]; update_fields.append("province")
        if data.address and data.address != "-":
            buyer.last_address = data.address; update_fields.append("last_address")
        buyer.order_count = ChannelBuyer.objects.filter(
            store=channel.store, platform=channel.platform, external_id=external_id
        ).values_list("order_count", flat=True).first() or 0
        buyer.order_count += 1
        update_fields.append("order_count")
        from decimal import Decimal
        buyer.total_spent = (buyer.total_spent or Decimal("0")) + order.total
        update_fields.append("total_spent")
        if update_fields:
            buyer.save(update_fields=update_fields + ["last_seen"])
    else:
        buyer.order_count = 1
        buyer.total_spent = order.total
        buyer.save(update_fields=["order_count", "total_spent"])

    # Link the ChannelOrder to this buyer for direct order lookups
    ChannelOrder.objects.filter(channel=channel, order=order).update(buyer=buyer)

    # Auto-create a pending Customer for merchant review (skipped if one already linked)
    _ensure_pending_customer(channel.store, buyer)


def _ensure_pending_customer(store, buyer):
    """Create a pending Customer linked to this ChannelBuyer if none exists yet."""
    if hasattr(buyer, "customer"):
        return  # already linked
    from apps.shops.models import Customer
    display_name = buyer.name or buyer.external_id
    # Truncate display name for Customer.name (max 120)
    Customer.objects.get_or_create(
        channel_buyer=buyer,
        defaults={
            "store": store,
            "name": display_name[:120],
            "phone": "",
            "status": Customer.STATUS_PENDING,
            "tags": buyer.platform,
        },
    )


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


# Thread-local batch context so import_order can find the current batch without arg drilling.
_current_batch: dict = {}


def import_orders(channel, orders, soft: bool = False,
                  file_name: str = "", imported_by=None, skipped: int = 0,
                  category: str = "regular"):
    """Import each order, create an ImportBatch record, return summary."""
    from decimal import Decimal
    batch = ImportBatch.objects.create(
        store=channel.store, channel=channel, imported_by=imported_by,
        file_name=file_name[:255], is_soft=soft,
        category=category if category in {c for c, _ in ImportBatch.CAT_CHOICES} else "regular",
    )
    _current_batch[channel.id] = batch

    summary = {"created": 0, "duplicates": 0, "errors": [], "soft": soft,
               "batch_id": str(batch.id)}
    total_amount = Decimal("0")

    try:
        for data in orders:
            try:
                order, created = import_order(channel, data, soft=soft)
            except ChannelImportError as error:
                summary["errors"].append(f"{data.external_id}: {error}")
                continue
            if created:
                summary["created"] += 1
                total_amount += order.total
            else:
                summary["duplicates"] += 1
    finally:
        _current_batch.pop(channel.id, None)

    # Update batch totals now that we know them
    batch.order_count = summary["created"]
    batch.duplicate_count = summary["duplicates"]
    batch.skipped_count = skipped
    batch.error_count = len(summary["errors"])
    batch.total_amount = total_amount
    batch.save(update_fields=["order_count", "duplicate_count", "skipped_count",
                               "error_count", "total_amount"])

    return summary


def get_or_create_channel(store, platform: str, name: str = "") -> "SalesChannel":
    """Return the first active channel for this platform, or create one automatically."""
    channel = SalesChannel.objects.filter(store=store, platform=platform, is_active=True).first()
    if not channel:
        channel = SalesChannel.objects.create(
            store=store, platform=platform,
            name=name or f"Toko {platform.capitalize()}",
            connection="csv",
        )
        match_listings(channel)
    return channel


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
