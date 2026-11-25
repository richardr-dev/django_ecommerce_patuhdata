import csv
import io
import logging
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
from apps.orders.inventory import commit_order_inventory, reserve_from_levels, reserve_variant
from apps.orders.models import Order, OrderItem, OrderStatusEvent, PaymentTransaction
from apps.tax.services import issue_invoice_for_order

from .adapters import (
    PLATFORMS,
    STATE_COMPLETED,
    STATE_SHIPPED,
    ChannelNotReady,
    MarketplaceOrder,
    OrderLine,
    adapter_for,
)
from .lifecycle import ORDER_STATUS, REVERSING, _note, sync_order_state
from .mapping import MappingError, expand_bundle, find_mapping, note_unmapped
from .models import ChannelBuyer, ChannelListing, ChannelOrder, ImportBatch, SalesChannel

CSV_COLUMNS = ["order_id", "buyer_name", "phone", "address", "sku", "quantity", "unit_price",
    "shipping_cost", "discount", "courier", "tracking_number"]


logger = logging.getLogger(__name__)


class ChannelImportError(ValueError):
    pass


class StockShortage(ChannelImportError):
    """The marketplace sold more of a SKU than the system holds."""


class UnknownSku(ChannelImportError):
    """A marketplace line whose SKU matches nothing in the catalog (or has no SKU at all)."""


def _resolve_sku(store, sku):
    variant = ProductVariant.objects.select_related("product").filter(product__store=store,
        sku=sku, is_active=True).first()
    if variant:
        return variant.product, variant
    product = Product.objects.filter(store=store, sku=sku).first()
    if not product:
        raise UnknownSku(f"SKU {sku} tidak ada di katalog Patuhdata.")
    if product.variants.filter(is_active=True).exists():
        raise ChannelImportError(f"SKU {sku} punya varian: gunakan SKU varian di marketplace.")
    return product, None


def import_order(channel, data: MarketplaceOrder, soft: bool = False, batch=None):
    """Import one marketplace order. Returns (order, created); see ``import_order_outcome``."""
    order, outcome = import_order_outcome(channel, data, soft=soft, batch=batch)
    return order, outcome == "created"


def import_order_outcome(channel, data: MarketplaceOrder, soft: bool = False, batch=None):
    """Create the paid Lakuin order for a marketplace order. Returns (order, created).

    soft=True: historical import mode — auto-creates placeholder products for unrecognised
    SKUs and skips stock deduction. Use for importing past orders when catalog isn't set up yet.
    soft=False (default): strict mode — raises ChannelImportError on unknown SKU or low stock.
    """
    existing = ChannelOrder.objects.filter(channel=channel,
        external_order_id=data.external_id).select_related("order").first()
    if existing:
        changed = sync_order_state(channel, existing.order, data)
        redated = apply_marketplace_dates(existing.order, data)
        return existing.order, "updated" if changed or redated else "duplicate"
    if not data.lines:
        raise ChannelImportError("Pesanan tanpa produk.")
    store = channel.store
    label = PLATFORMS[channel.platform].label

    if data.state in REVERSING:
        # Never seen, already cancelled or returned: keep it as a record, never as a sale.
        return _record_dead_order(channel, data, batch), "created"
    resolved = resolve_lines(channel, data.lines, soft=soft)

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
                    variant_name=variant.name if variant else "", unit_price=line.unit_price, bundle_sku=line.bundle,
                    quantity=line.quantity, line_total=_line_total(line))
                if not soft:
                    _reserve(item, product, variant, line.quantity)
            confirm_payment(order, provider=channel.platform, external_id=data.external_id,
                amount=order.total, currency="IDR", payload=data.raw,
                note=f"Dibayar melalui {label}")
            ChannelOrder.objects.create(channel=channel, order=order,
                external_order_id=data.external_id,
                batch=batch)
            _upsert_buyer(channel, data, order)
            apply_marketplace_dates(order, data)
    except IntegrityError:  # imported concurrently
        existing = ChannelOrder.objects.filter(channel=channel,
            external_order_id=data.external_id).select_related("order").first()
        if existing:
            return existing.order, "duplicate"
        raise
    # Invoice the sale (dated by the order) and take its stock. A failure here never loses the
    # import: the order stays, the error is logged, and the books' worker issues the missing
    # invoice later (see ``tax.posting.issue_missing_invoices``).
    try:
        issue_invoice_for_order(order, notes=event_note,
            issued_on=timezone.localdate(data.placed_at) if data.placed_at else None)
    except Exception:  # noqa: BLE001 - see above
        logger.exception("invoice for marketplace order %s could not be issued", order.pk)
    if not soft:
        try:
            commit_order_inventory(order)
        except Exception:
            pass  # low stock is non-fatal; stock was already reserved
    if data.state in {STATE_SHIPPED, STATE_COMPLETED}:
        sync_order_state(channel, order, data)
    return order, "created"


def apply_marketplace_dates(order, data):
    """Date the order, its payment and its invoice by the marketplace's own order time, not by
    the day the file was imported: the sale belongs to the month the buyer ordered it (PPN,
    PPh Final and the profit and loss all follow). Returns True when anything moved."""
    from apps.accounting.ledger import redate_payment
    from apps.tax.models import Invoice
    if not data.placed_at:
        return False
    placed, paid = data.placed_at, data.paid_at or data.placed_at
    day, moved = timezone.localdate(placed), False
    if timezone.localdate(order.created_at) != day:
        Order.objects.filter(pk=order.pk).update(created_at=placed)
        order.created_at, moved = placed, True
    for payment in order.payment_transactions.filter(kind="payment"):
        if timezone.localdate(payment.updated_at) != timezone.localdate(paid):
            PaymentTransaction.objects.filter(pk=payment.pk).update(created_at=paid, updated_at=paid)
            payment.updated_at = paid
            redate_payment(payment, timezone.localdate(paid))
            moved = True
    invoice = Invoice.objects.filter(order=order).first()
    if invoice and invoice.issued_on != day:
        Invoice.objects.filter(pk=invoice.pk).update(issued_on=day)
        moved = True
    if moved:
        OrderStatusEvent.objects.create(order=order, status=order.status,
            note=f"Tanggal disesuaikan dengan waktu pesanan di marketplace ({day:%d/%m/%Y})")
    return moved


def _record_dead_order(channel, data, batch):
    """An order that reached us already cancelled or returned: kept so cancellations can be
    counted per channel, with no stock, payment, invoice or ledger entry."""
    store, label = channel.store, PLATFORMS[channel.platform].label
    resolved = resolve_lines(channel, data.lines, soft=True)
    with transaction.atomic():
        order = Order.objects.create(store=store, channel=channel.platform,
            external_order_id=data.external_id[:100], status=ORDER_STATUS[data.state],
            marketplace_status=data.status_text[:60],
            shipping_service=data.courier[:80], tracking_number=data.tracking_number[:100],
            customer_name=(data.buyer_name or f"Pembeli {label}")[:120],
            phone=data.phone[:30] or "-", shipping_address=data.address or "-",
            subtotal=data.subtotal, discount=min(data.discount, data.subtotal),
            shipping_cost=data.shipping_cost, total=data.total, payment_provider=channel.platform,
            payment_method="marketplace", paid_to_store_account=True,
            idempotency_key=f"ch{channel.id}-{data.external_id}"[:64])
        OrderStatusEvent.objects.create(order=order, status=order.status,
            note=_note(channel, data.state, data.status_text) + " · diimpor tanpa penjualan")
        for line, product, variant in resolved:
            OrderItem.objects.create(order=order, product=product, variant=variant,
                name=(line.name or product.name)[:160], sku=line.sku,
                variant_name=variant.name if variant else "", unit_price=line.unit_price, bundle_sku=line.bundle,
                quantity=line.quantity, line_total=_line_total(line))
        ChannelOrder.objects.create(channel=channel, order=order,
            external_order_id=data.external_id, batch=batch)
        if data.placed_at:
            Order.objects.filter(pk=order.pk).update(created_at=data.placed_at)
            order.created_at = data.placed_at
    return order


def _line_total(line):
    return line.line_total if line.line_total is not None else line.unit_price * line.quantity


def resolve_line(channel, line, *, soft=False):
    """Match one marketplace line to the catalog: the channel's SKU map, a bundle, then the
    master SKU. A SKU that matches nothing is queued for review, then refused (strict) or given
    a placeholder product (historical mode). Returns ``[(OrderLine, product, variant)]``."""
    product, variant, bundle = find_mapping(channel, line.sku)
    if bundle:
        try:
            return expand_bundle(bundle, line)
        except MappingError as error:
            raise ChannelImportError(str(error)) from error
    if product:
        return [(line, product, variant)]
    try:
        found = _resolve_sku(channel.store, line.sku)
    except ChannelImportError:
        if not soft:
            note_unmapped(channel, line)
            raise
        found = _resolve_sku_soft(channel.store, line)
        note_unmapped(channel, line, placeholder=found[0])
    return [(line, *found)]


def resolve_lines(channel, lines, *, soft=False):
    return [row for line in lines for row in resolve_line(channel, line, soft=soft)]


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
    candidate_slug = (slugify(sku) if sku else slugify(base_name))[:44]  # slug is 50 characters: room for "-12"
    # Ensure slug uniqueness by appending a suffix when collisions exist
    slug = candidate_slug
    suffix = 1
    while Product.objects.filter(store=store, slug=slug).exists():
        slug = f"{candidate_slug}-{suffix}"
        suffix += 1

    name = (f"{base_name} [{sku}]" if sku else base_name)[:160]

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
    external_id = (raw.get("buyer_username") or raw.get("shopee_username")
                   or raw.get("tokopedia_username")
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
        buyer.total_spent = (buyer.total_spent or Decimal(0)) + order.total
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
    # A buyer profile that was cleared and rebuilt finds the customer it had before: the code is unique.
    code = f"{buyer.platform.upper()}-{buyer.external_id[:40]}"
    orphan = Customer.objects.filter(store=store, customer_code=code, channel_buyer__isnull=True).first()
    if orphan:
        orphan.channel_buyer = buyer
        orphan.save(update_fields=["channel_buyer"])
        return
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
            raise StockShortage(_shortage(item.sku, quantity, variant.available_stock))
        reserve_variant(item, variant, quantity)
        return
    levels = list(InventoryLevel.objects.select_for_update().filter(product=product,
        warehouse__store=product.store, warehouse__is_active=True)
        .order_by("warehouse__priority", "warehouse_id"))
    available = sum(level.available for level in levels)
    if available < quantity:
        # The marketplace already sold it. A store that allows stok minus records the shortfall
        # instead of refusing the import; the next goods receipt covers it and corrects the cost.
        if not (levels and product.store.allow_negative_stock and product.cost_price is not None):
            raise StockShortage(_shortage(item.sku, quantity, available))
        from apps.orders.deficits import sell_short
        if available > 0:
            reserve_from_levels(item, levels, available, reason="marketplace_order")
        sell_short(item, levels[0], quantity - available)
        return
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


# The states a file import records when it takes finished orders only: the sale is done, or it was
# called off. Anything in between (packing, shipped, in transit) waits for a later file.
IMPORT_FINISHED = {STATE_COMPLETED} | REVERSING


def import_orders(channel, orders, soft: bool = False,
                  file_name: str = "", imported_by=None, skipped: int = 0,
                  category: str = "regular", finished_only: bool = False, include_cancelled: bool = True,
                  states=None):
    """Import each order, create an ImportBatch record, return summary.

    ``finished_only``: a marketplace file lists orders at every stage. An order the marketplace calls
    finished (Selesai, Berhasil) becomes a sale, and a cancelled or returned one is recorded with its
    status but is never a sale. One still being packed or shipped is left for a later file. An order
    imported earlier is always brought up to date, so a return after completion takes the sale back.

    ``include_cancelled=False``: a cancelled or returned order that was never imported is skipped (counted in
    ``cancelled_skipped``) instead of being recorded. One already imported is still updated, whatever the
    option says, because a cancellation after the sale must take the sale back.

    ``states``: the person's own choice of which marketplace statuses to import (a set of ``STATE_*``
    values, e.g. completed, shipped, active, cancelled, returned). It replaces ``finished_only`` and
    ``include_cancelled``. An order of any other status that was never imported is left for a later file;
    one already imported is still brought up to date."""
    from decimal import Decimal
    if states is not None:
        states = set(states)
        finished_only = True
        include_cancelled = bool(states & REVERSING)
        wanted = states | REVERSING if include_cancelled else states
    else:
        wanted = IMPORT_FINISHED
    batch = ImportBatch.objects.create(
        store=channel.store, channel=channel, imported_by=imported_by,
        file_name=file_name[:255], is_soft=soft,
        category=category if category in {c for c, _ in ImportBatch.CAT_CHOICES} else "regular",
    )

    summary = {"created": 0, "duplicates": 0, "updated": 0, "cancelled": 0, "errors": [],
               "soft": soft, "not_finished": 0, "no_stock": 0, "new_products": 0, "cancelled_skipped": 0,
               "batch_id": str(batch.id)}
    total_amount = Decimal(0)
    known = set()
    if finished_only or not include_cancelled:
        ids = [data.external_id for data in orders]
        for start in range(0, len(ids), 500):
            known.update(ChannelOrder.objects.filter(channel=channel,
                external_order_id__in=ids[start:start + 500]).values_list("external_order_id", flat=True))

    for data in orders:
        if not include_cancelled and data.state in REVERSING and data.external_id not in known:
            summary["cancelled_skipped"] += 1
            continue
        if finished_only and data.state not in wanted and data.external_id not in known:
            summary["not_finished"] += 1
            continue
        try:
            order, outcome = import_order_outcome(channel, data, soft=soft, batch=batch)
        except (StockShortage, UnknownSku) as error:
            # A finished order is a sale that already happened: refusing it only hides it. Record it
            # without taking stock (as in historical mode) and flag it, so someone corrects the stock
            # or maps the SKU (Pemetaan SKU).
            if not (finished_only and data.state in wanted and data.state not in REVERSING):
                summary["errors"].append(f"{data.external_id}: {error}")
                continue
            try:
                order, outcome = import_order_outcome(channel, data, soft=True, batch=batch)
            except ChannelImportError as again:
                summary["errors"].append(f"{data.external_id}: {again}")
                continue
            if isinstance(error, UnknownSku):
                note = f"{error} Produk dibuat otomatis: cocokkan di Pemetaan SKU. Stok belum dikurangi."
                summary["new_products"] += 1
            else:
                note = f"{error} Penjualan dicatat, stok belum dikurangi."
                summary["no_stock"] += 1
            ChannelOrder.objects.filter(order=order).update(attention=note[:300])
        except ChannelImportError as error:
            summary["errors"].append(f"{data.external_id}: {error}")
            continue
        if outcome == "created" and data.state in REVERSING:
            summary["cancelled"] += 1  # recorded, but not a sale
        elif outcome == "created":
            summary["created"] += 1
            total_amount += order.total
        elif outcome == "updated":
            summary["updated"] += 1
        else:
            summary["duplicates"] += 1
        if data.state in REVERSING and outcome == "updated":
            summary["cancelled"] += 1

    # Update batch totals now that we know them
    batch.order_count = summary["created"]
    batch.duplicate_count = summary["duplicates"]
    batch.updated_count = summary["updated"]
    batch.cancelled_count = summary["cancelled"]
    batch.skipped_count = skipped + summary["not_finished"] + summary["cancelled_skipped"]
    batch.error_count = len(summary["errors"])
    batch.total_amount = total_amount
    batch.save(update_fields=["order_count", "duplicate_count", "updated_count", "cancelled_count",
                               "skipped_count",
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
