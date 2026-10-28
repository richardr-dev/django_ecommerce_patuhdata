import csv
import io
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import (
    Category,
    InventoryLevel,
    Product,
    ProductVariant,
    Warehouse,
    _lock_store,
)
from apps.shops.models import AuditLog, Store

from .models import ShippingMethod


def _setup_store(request):
    stores = (Store.objects.filter(owner=request.user) |
        Store.objects.filter(staff_members__user=request.user)).distinct().order_by("name")
    requested = request.GET.get("store") or request.POST.get("store")
    if requested and stores.filter(id=requested).exists():
        request.session["merchant_store_id"] = int(requested)
    store = stores.filter(id=request.session.get("merchant_store_id")).first() or stores.first()
    if not store:
        raise Http404
    role = "admin" if store.owner_id == request.user.id else store.staff_members.get(user=request.user).role
    if role not in {"admin", "manager", "ops"}:
        raise Http404
    request.session["merchant_store_id"] = store.id
    return store, stores, role


MAX_IMPORT_ROWS = 10000
MAX_IMPORT_BYTES = 10 * 1024 * 1024
BULK_BATCH_SIZE = 500


def parse_import_rows(rows):
    """Validate every row before touching the database, so a bad row fails fast."""
    parsed, seen = [], {}
    for number, row in enumerate(rows, 2):
        name, sku = (row.get("name") or "").strip(), (row.get("sku") or "").strip()[:80]
        if not name or not sku:
            raise ValueError(f"Baris {number}: name dan sku wajib diisi.")
        if sku in seen:
            raise ValueError(f"Baris {number}: SKU {sku} sudah muncul di baris {seen[sku]}.")
        seen[sku] = number
        try:
            price = Decimal((row.get("price") or "").strip())
            stock = int((row.get("stock") or "").strip())
            weight = int((row.get("weight_grams") or "0").strip() or 0)
        except (InvalidOperation, ValueError):
            raise ValueError(f"Baris {number}: price, stock, atau weight_grams tidak valid.") from None
        if not price.is_finite() or price < 0 or stock < 0 or weight < 0:
            raise ValueError(f"Baris {number}: angka tidak boleh negatif.")
        if price >= 10 ** 12:
            raise ValueError(f"Baris {number}: price terlalu besar.")
        parsed.append({"number": number, "sku": sku, "stock": stock,
            "category": (row.get("category") or "").strip(),
            "values": {"name": name[:160], "price": price, "weight_grams": weight,
                "description": (row.get("description") or "").strip(),
                "image_url": (row.get("image_url") or "").strip()[:200], "is_active": True}})
    return parsed


def import_products(store, parsed):
    """Upsert products by SKU with a fixed number of queries per batch rather than per row.
    The store row lock serialises this with Product.save()'s SKU uniqueness check."""
    _lock_store(store.id)
    warehouse, _ = Warehouse.objects.get_or_create(store=store, code="MAIN",
        defaults={"name": "Gudang Utama", "priority": 10})
    skus = [row["sku"] for row in parsed]
    variant_skus = set(ProductVariant.objects.filter(product__store=store, sku__in=skus)
        .values_list("sku", flat=True))
    if variant_skus:
        row = next(row for row in parsed if row["sku"] in variant_skus)
        raise ValueError(f"Baris {row['number']}: SKU {row['sku']} sudah dipakai varian "
            "produk lain di toko ini.")

    categories = {category.slug: category for category in Category.objects.filter(store=store)}
    missing = {}
    for row in parsed:
        if row["category"]:
            slug = slugify(row["category"])[:50] or "category"
            row["category_slug"] = slug
            if slug not in categories:
                missing.setdefault(slug, Category(store=store, slug=slug, name=row["category"][:100]))
    if missing:
        Category.objects.bulk_create(missing.values(), batch_size=BULK_BATCH_SIZE)
        categories = {category.slug: category for category in Category.objects.filter(store=store)}

    existing = {product.sku: product for product in Product.objects.filter(store=store, sku__in=skus)}
    taken_slugs = set(Product.objects.filter(store=store).values_list("slug", flat=True))
    to_create, to_update = [], []
    for row in parsed:
        values = {**row["values"], "category": categories.get(row.get("category_slug"))}
        product = existing.get(row["sku"])
        if product:
            for field, value in values.items():
                setattr(product, field, value)
            to_update.append(product)
            continue
        base_slug = slugify(values["name"])[:45] or slugify(row["sku"])[:45] or "product"
        candidate, suffix = base_slug, 2
        while candidate in taken_slugs:
            candidate, suffix = f"{base_slug}-{suffix}", suffix + 1
        taken_slugs.add(candidate)
        to_create.append(Product(store=store, sku=row["sku"], slug=candidate, **values))
    Product.objects.bulk_update(to_update, ["name", "price", "category", "description",
        "weight_grams", "image_url", "is_active"], batch_size=BULK_BATCH_SIZE)
    Product.objects.bulk_create(to_create, batch_size=BULK_BATCH_SIZE)

    product_ids = dict(Product.objects.filter(store=store, sku__in=skus).values_list("sku", "id"))
    levels = {level.product_id: level for level in InventoryLevel.objects.select_for_update()
        .filter(warehouse=warehouse, product_id__in=product_ids.values())}
    now, new_levels, changed_levels = timezone.now(), [], []
    for row in parsed:
        product_id = product_ids[row["sku"]]
        level = levels.get(product_id)
        if level is None:
            new_levels.append(InventoryLevel(warehouse=warehouse, product_id=product_id,
                on_hand=row["stock"]))
        else:
            level.on_hand, level.updated_at = max(row["stock"], level.reserved), now
            changed_levels.append(level)
    InventoryLevel.objects.bulk_create(new_levels, batch_size=BULK_BATCH_SIZE)
    InventoryLevel.objects.bulk_update(changed_levels, ["on_hand", "updated_at"],
        batch_size=BULK_BATCH_SIZE)
    return len(to_create), len(to_update)


@login_required(login_url="/merchant/login/")
def merchant_quick_defaults(request):
    if request.method != "POST":
        raise Http404
    store, _, _ = _setup_store(request)
    with transaction.atomic():
        _warehouse, warehouse_created = Warehouse.objects.get_or_create(store=store, code="MAIN",
            defaults={"name": "Gudang Utama", "priority": 10, "is_active": True})
        _shipping, shipping_created = ShippingMethod.objects.get_or_create(store=store, code="reguler",
            defaults={"name": "Pengiriman Reguler", "provider": "manual", "flat_rate": 15000,
                "eta": "2–4 hari kerja", "is_active": True})
        AuditLog.objects.create(store=store, actor=request.user, action="store.quick_defaults_created",
            object_type="Store", object_id=str(store.id), details={"warehouse": warehouse_created,
                "shipping": shipping_created})
    created = sum((warehouse_created, shipping_created))
    messages.success(request, f"Setup operasional siap. {created} data bawaan dibuat.")
    return redirect("merchant-settings")


@login_required(login_url="/merchant/login/")
def merchant_product_import_template(request):
    _setup_store(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="template-produk.csv"'
    writer = csv.writer(response)
    writer.writerow(["name", "sku", "price", "stock", "category", "description", "weight_grams", "image_url"])
    writer.writerow(["Contoh Produk", "SKU-001", "99000", "10", "Kategori", "Deskripsi produk", "500", "https://example.com/image.jpg"])
    return response


@login_required(login_url="/merchant/login/")
def merchant_product_import(request):
    store, stores, role = _setup_store(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Pilih file CSV terlebih dahulu.")
        elif upload.size > MAX_IMPORT_BYTES:
            messages.error(request, "Ukuran CSV maksimal 10 MB.")
        else:
            try:
                text = upload.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                required = {"name", "sku", "price", "stock"}
                if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                    raise ValueError("Kolom wajib: name, sku, price, stock.")
                rows = list(reader)
                if not rows:
                    raise ValueError("CSV tidak berisi produk.")
                if len(rows) > MAX_IMPORT_ROWS:
                    raise ValueError(f"Maksimal {MAX_IMPORT_ROWS:,} produk per impor.".replace(",", "."))
                parsed = parse_import_rows(rows)
                with transaction.atomic():
                    created, updated = import_products(store, parsed)
                    AuditLog.objects.create(store=store, actor=request.user, action="products.csv_imported",
                        object_type="Product", object_id="bulk", details={"created": created,
                            "updated": updated, "rows": len(rows)})
                messages.success(request, f"Impor selesai: {created} produk dibuat, {updated} diperbarui.")
                return redirect("merchant-entity-list", section="products")
            except (UnicodeDecodeError, csv.Error, ValueError) as error:
                messages.error(request, str(error))
    context = {"merchant_store": store, "merchant_stores": stores, "merchant_role": role}
    return render(request, "merchant/product_import.html", context)
