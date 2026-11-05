"""Pesanan manual: the seller creates an order for a customer who ordered by chat, and sends
them a payment link. Stock is reserved at once, exactly as checkout reserves it, and is
released by the expiry worker when the customer does not pay in time.

A gateway payment session is opened only when the customer presses "Bayar sekarang", because
gateway pages expire within the hour while the link may be sent days ahead."""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.integrations.models import OutboxEvent
from apps.integrations.payments import order_payment_provider, payment_options, reserved_reference
from apps.shops.models import AuditLog, Customer
from apps.shops.tenancy import store_url

from .inventory import reserve_from_levels, reserve_variant
from .models import Order, OrderItem, OrderStatusEvent, PaymentTransaction
from .pricing import calculate_price
from .services import normalize_indonesian_phone
from .wholesale import product_quantities, tier_for, tier_unit_price

MAX_LINES = 50


class ManualOrderError(Exception):
    pass


class ManualOrderForm(forms.Form):
    customer_name = forms.CharField(label="Nama pelanggan", max_length=120)
    phone = forms.CharField(label="Nomor WhatsApp", max_length=30,
        widget=forms.TextInput(attrs={"inputmode": "tel", "placeholder": "081234567890"}))
    email = forms.EmailField(label="Email (opsional)", required=False)
    fulfilment = forms.ChoiceField(label="Pengiriman", choices=[("ship", "Dikirim ke alamat")])
    shipping_address = forms.CharField(label="Alamat lengkap", required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Gedung/jalan, nomor, RT/RW"}))
    village = forms.CharField(label="Kelurahan", max_length=100, required=False)
    district = forms.CharField(label="Kecamatan", max_length=100, required=False)
    city = forms.CharField(label="Kota/Kabupaten", max_length=100, required=False)
    province = forms.CharField(label="Provinsi", max_length=100, required=False)
    postal_code = forms.CharField(label="Kode pos", max_length=10, required=False)
    shipping_name = forms.CharField(label="Layanan pengiriman", max_length=80, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Contoh: JNE REG"}))
    shipping_cost = forms.DecimalField(label="Ongkir", min_value=0, max_digits=12,
        decimal_places=0, initial=0)
    discount = forms.DecimalField(label="Potongan harga", min_value=0, max_digits=12,
        decimal_places=0, initial=0, required=False)
    payment_method = forms.ChoiceField(label="Metode pembayaran")
    valid_days = forms.TypedChoiceField(label="Link berlaku", coerce=int, initial=2,
        choices=[(1, "1 hari"), (2, "2 hari"), (3, "3 hari"), (7, "7 hari")])
    note = forms.CharField(label="Catatan untuk pelanggan (opsional)", max_length=300,
        required=False)
    idempotency_key = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store
        self.options = payment_options(store)
        self.fields["payment_method"].choices = [(option.code, option.label)
            for option in self.options]
        self.outlets = list(Warehouse.objects.filter(store=store, is_active=True, is_outlet=True))
        self.fields["fulfilment"].choices += [(f"outlet-{outlet.id}", f"Ambil di {outlet.name}")
            for outlet in self.outlets]
        self.fields["idempotency_key"].initial = uuid.uuid4().hex
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")

    def clean_phone(self):
        try:
            return normalize_indonesian_phone(self.cleaned_data["phone"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None

    def clean(self):
        cleaned = super().clean()
        if not self.options:
            raise forms.ValidationError("Aktifkan metode pembayaran dulu di menu Pembayaran.")
        cleaned["pickup_outlet"] = next((outlet for outlet in self.outlets
            if cleaned.get("fulfilment") == f"outlet-{outlet.id}"), None)
        if not cleaned["pickup_outlet"] and not (cleaned.get("shipping_address") or "").strip():
            self.add_error("shipping_address", "Isi alamat pengiriman, atau pilih ambil di outlet.")
        cleaned["payment_option"] = next((option for option in self.options
            if option.code == cleaned.get("payment_method")), None)
        cleaned["discount"] = cleaned.get("discount") or Decimal(0)
        return cleaned


def parse_lines(post):
    """Item rows posted as sku / quantity / price lists. Returns (rows, errors)."""
    rows, errors = [], []
    skus, quantities, prices = post.getlist("sku"), post.getlist("quantity"), post.getlist("price")
    for index, sku in enumerate(skus[:MAX_LINES]):
        sku = sku.strip()
        if not sku:
            continue
        try:
            quantity = int(quantities[index]) if index < len(quantities) else 1
        except ValueError:
            quantity = 0
        price_text = (prices[index] if index < len(prices) else "").strip().replace(".", "")
        try:
            price = Decimal(price_text) if price_text else None
        except InvalidOperation:
            price = Decimal(-1)
        if quantity < 1:
            errors.append(f"{sku}: jumlah minimal 1.")
        elif price is not None and price < 0:
            errors.append(f"{sku}: harga tidak valid.")
        else:
            rows.append({"sku": sku, "quantity": quantity, "price": price})
    if not rows and not errors:
        errors.append("Tambahkan minimal satu produk.")
    return rows, errors


def _resolve(store, rows):
    """Products/variants for the rows, locked. Raises ManualOrderError on unknown SKUs."""
    skus = [row["sku"] for row in rows]
    variants = {variant.sku: variant for variant in ProductVariant.objects.select_for_update()
        .filter(product__store=store, sku__in=skus, is_active=True).select_related("product")}
    products = {product.sku: product for product in Product.objects.select_for_update()
        .filter(store=store, sku__in=skus)}
    lines, errors = [], []
    for row in rows:
        variant = variants.get(row["sku"])
        product = variant.product if variant else products.get(row["sku"])
        if product is None:
            errors.append(f"SKU {row['sku']} tidak ditemukan.")
            continue
        if variant is None and product.variants.filter(is_active=True).exists():
            errors.append(f"{product.name} punya varian: tulis SKU variannya.")
            continue
        regular = variant.effective_price if variant else product.price
        unit_price = row["price"] if row["price"] is not None else regular
        lines.append({"product": product, "variant": variant, "quantity": row["quantity"],
            "unit_price": unit_price, "priced": row["price"] is not None})
    if errors:
        raise ManualOrderError(" ".join(errors))
    # Harga grosir for rows without a typed price, by each product's total quantity.
    quantities = product_quantities(lines)
    for line in lines:
        if not line.pop("priced"):
            tier = tier_for(line["product"], quantities[line["product"].id])
            line["unit_price"] = tier_unit_price(line["product"], line["variant"], tier)
        line["total"] = line["unit_price"] * line["quantity"]
    return lines


def create_manual_order(store, user, data, rows):
    """Create the order, reserve its stock, and return it. Raises ManualOrderError."""
    existing = Order.objects.filter(store=store, idempotency_key=data["idempotency_key"]).first()
    if existing:
        return existing  # a double click on "Buat pesanan"
    option, outlet = data["payment_option"], data["pickup_outlet"]
    with transaction.atomic():
        lines = _resolve(store, rows)
        product_ids = [line["product"].id for line in lines if not line["variant"]]
        levels = list(InventoryLevel.objects.select_for_update().filter(
            product_id__in=product_ids, warehouse__store=store, warehouse__is_active=True)
            .select_related("warehouse").order_by("warehouse__priority", "warehouse_id"))
        if outlet:
            levels = [level for level in levels if level.warehouse_id == outlet.id]
        by_product = {product_id: [] for product_id in product_ids}
        for level in levels:
            by_product[level.product_id].append(level)
        needed = {}
        for line in lines:
            key = ("v", line["variant"].id) if line["variant"] else ("p", line["product"].id)
            needed[key] = needed.get(key, 0) + line["quantity"]
        for line in lines:
            if line["product"].is_preorder or line["product"].is_digital:
                continue
            key = ("v", line["variant"].id) if line["variant"] else ("p", line["product"].id)
            available = (line["variant"].available_stock if line["variant"] else
                sum(level.available for level in by_product[line["product"].id]))
            if available < needed[key]:
                name = line["product"].name + (f" · {line['variant'].name}" if line["variant"] else "")
                raise ManualOrderError(f"Stok {name} tidak cukup (tersedia {available})."
                    + (f" di {outlet.name}" if outlet else ""))
        shipping = Decimal(0) if outlet else data["shipping_cost"]
        price = calculate_price(store=store, lines=lines, shipping_cost=shipping,
            additional_discount=data["discount"])
        deadline = timezone.now() + timedelta(days=data["valid_days"])
        order = Order.objects.create(store=store, source="manual", created_by=user,
            subtotal=price.subtotal, tax=price.tax, shipping_cost=price.shipping,
            discount=price.discount, total=price.total,
            customer_name=data["customer_name"].strip(), email=data.get("email", "").strip(),
            phone=data["phone"], pickup_outlet=outlet,
            shipping_address=(data.get("shipping_address", "").strip() or
                f"Ambil di {outlet.name}: {outlet.address}".strip(": ")),
            village=data.get("village", "").strip(), district=data.get("district", "").strip(),
            city=data.get("city", "").strip(), province=data.get("province", "").strip(),
            postal_code=data.get("postal_code", "").strip(),
            delivery_notes=data.get("note", "").strip(),
            payment_provider=option.provider, payment_method=option.code,
            paid_to_store_account=option.store_account,
            shipping_provider="pickup" if outlet else "manual",
            courier_code=f"outlet-{outlet.id}" if outlet else "manual",
            shipping_service=(f"Ambil di {outlet.name}" if outlet else
                data.get("shipping_name", "").strip() or "Pengiriman oleh penjual"),
            payment_expires_at=deadline, idempotency_key=data["idempotency_key"])
        for line in lines:
            product, variant = line["product"], line["variant"]
            item = OrderItem.objects.create(order=order, product=product, variant=variant,
                name=product.name, sku=variant.sku if variant else product.sku,
                variant_name=variant.name if variant else "", unit_price=line["unit_price"],
                quantity=line["quantity"], line_total=line["total"], is_preorder=product.is_preorder)
            if product.is_preorder or product.is_digital:
                continue
            if variant:
                reserve_variant(item, variant, line["quantity"])
            else:
                reserve_from_levels(item, by_product[product.id], line["quantity"])
        order.payment_reference = reserved_reference(order_payment_provider(order), order)
        order.save(update_fields=["payment_reference"])
        OrderStatusEvent.objects.create(order=order, status="pending",
            note=f"Pesanan manual dibuat oleh {user.get_full_name() or user.username}")
        Customer.objects.update_or_create(store=store, phone=order.phone,
            defaults={"name": order.customer_name, **({"email": order.email} if order.email else {})})
        OutboxEvent.objects.create(store=store, topic="order.created", aggregate_id=str(order.id),
            payload={"order_id": str(order.id), "phone": order.phone, "email": order.email,
                "total": str(order.total), "payment_method": order.payment_method})
        AuditLog.objects.create(store=store, actor=user, action="order.manual_created",
            object_type="Order", object_id=str(order.id),
            details={"total": str(order.total), "items": len(lines)})
    if order.payment_provider == "bank_transfer":
        start_payment(order)  # no gateway call: the order page shows the bank details
    return order


def start_payment(order):
    """Open the payment for a pending order that has none yet. Returns the URL to send the
    customer to. Raises ProviderError when the gateway refuses."""
    if order.payment_checkout_url:
        return order.payment_checkout_url
    payment = order_payment_provider(order).create_payment(order)
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(id=order.id)
        if locked.status != "pending":
            return payment_page(locked)
        if locked.payment_checkout_url:
            return locked.payment_checkout_url
        locked.payment_reference = payment.reference
        locked.payment_checkout_url = payment.checkout_url
        expires = parse_datetime(payment.expires_at) if payment.expires_at else None
        if expires:
            # A gateway page ends sooner than the link; bank transfer keeps the longer deadline.
            locked.payment_expires_at = (max(expires, locked.payment_expires_at)
                if locked.payment_provider == "bank_transfer" and locked.payment_expires_at
                else expires)
        locked.save(update_fields=["payment_reference", "payment_checkout_url",
            "payment_expires_at"])
        PaymentTransaction.objects.create(order=locked, provider=locked.payment_provider,
            external_id=payment.reference, status="pending", amount=locked.total, currency="IDR")
    order.refresh_from_db()
    return order.payment_checkout_url


def payment_page(order):
    """The link sent to the customer: their order page, which opens the payment."""
    return store_url(order.store, f"/orders/{order.id}/?token={order.access_token}")


def whatsapp_message(order):
    lines = [f"Halo {order.customer_name}, berikut pesanan Anda di {order.store.name}:"]
    for item in order.items.all():
        name = item.name + (f" ({item.variant_name})" if item.variant_name else "")
        lines.append(f"• {item.quantity}× {name}")
    lines.append(f"Total: Rp{order.total:,.0f}".replace(",", "."))
    if order.payment_expires_at:
        lines.append(f"Bayar sebelum {timezone.localtime(order.payment_expires_at):%d %b %Y %H:%M}:")
    lines.append(payment_page(order))
    return "\n".join(lines)


def whatsapp_url(order):
    from urllib.parse import quote
    phone = order.phone.removeprefix("0")
    return f"https://wa.me/62{phone}?text={quote(whatsapp_message(order))}"
