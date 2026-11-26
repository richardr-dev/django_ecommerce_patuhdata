import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from apps.billing.plans import product_limit_error
from apps.catalog.models import (
    InventoryLevel,
    Product,
    ProductVariant,
    Warehouse,
    _lock_store,
)
from apps.shops.models import AuditLog, Store
from apps.shops.tenancy import store_url

from .models import InventoryMovement, ShippingMethod


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


# Column names a catalogue export may use (ours, Indonesian, Accurate's item list) → our field.
HEADER_ALIASES = {
    "name": ("name", "nama", "nama produk", "nama barang", "item name", "item description", "product name"),
    "sku": ("sku", "kode", "kode barang", "kode produk", "item no", "item no.", "no. barang", "seller sku"),
    "price": ("price", "harga", "harga jual", "unit price", "unitprice", "sales price"),
    "stock": ("stock", "stok", "qty", "quantity", "kuantitas", "jumlah"),
    "category": ("category", "kategori", "item category", "kategori barang"),
    "description": ("description", "deskripsi", "keterangan"),
    "weight_grams": ("weight_grams", "berat", "berat (gram)", "weight", "berat gram"),
    "image_url": ("image_url", "foto", "url foto", "gambar", "image"),
    "cost_price": ("cost_price", "harga pokok", "harga modal", "hpp", "unit cost", "cost", "harga beli"),
    "length_cm": ("length_cm", "panjang", "panjang (cm)", "length", "p (cm)"),
    "width_cm": ("width_cm", "lebar", "lebar (cm)", "width", "l (cm)"),
    "height_cm": ("height_cm", "tinggi", "tinggi (cm)", "height", "t (cm)"),
    "dimensions": ("dimensions", "dimensi", "dimensi (cm)", "ukuran paket", "p x l x t", "pxlxt"),
    "brand": ("brand", "merek", "merk"),
    "barcode": ("barcode", "kode barcode", "ean", "upc"),
    "color": ("color", "colour", "warna"),
    "size": ("size", "ukuran"),
    "material": ("material", "bahan"),
}
HEADER_ALIASES.update({
    "condition": ("condition", "kondisi", "kondisi produk"),
    "model_number": ("model_number", "model", "no. artikel", "nomor artikel", "kode model"),
    "min_order": ("min_order", "minimal pembelian", "min. pembelian", "min order", "minimum pembelian"),
    "max_order": ("max_order", "maksimal pembelian", "maks. pembelian", "max order", "batas pembelian"),
    "country_of_origin": ("country_of_origin", "negara asal", "asal negara", "country of origin"),
    "warranty_type": ("warranty_type", "jenis garansi", "tipe garansi"),
    "warranty_months": ("warranty_months", "masa garansi (bulan)", "masa garansi", "garansi (bulan)"),
    "shelf_life_days": ("shelf_life_days", "masa simpan (hari)", "masa simpan", "masa kedaluwarsa (hari)"),
    "bpom_number": ("bpom_number", "no. bpom", "nomor bpom", "bpom"),
    "halal_number": ("halal_number", "no. sertifikat halal", "sertifikat halal", "no. halal", "halal"),
    "sni_number": ("sni_number", "no. sni", "nomor sni", "sni"),
    "dangerous_goods": ("dangerous_goods", "barang berbahaya", "dangerous goods"),
    "insurance_required": ("insurance_required", "wajib asuransi", "asuransi"),
    "showcase": ("showcase", "etalase", "etalase toko"),
    "video_url": ("video_url", "video", "link video", "url video"),
    "size_chart_url": ("size_chart_url", "tabel ukuran", "size chart", "link tabel ukuran"),
    "shopee_category": ("shopee_category", "kategori shopee"),
    "tokopedia_category": ("tokopedia_category", "kategori tokopedia", "kategori tiktok", "kategori tiktok shop"),
})
ATTRIBUTE_PREFIXES = ("atribut:", "atribut ", "attribute:")
TEXT_FIELDS = ("brand", "barcode", "color", "size", "material", "model_number", "country_of_origin", "bpom_number",
    "halal_number", "sni_number", "showcase", "shopee_category", "tokopedia_category")
URL_FIELDS = ("video_url", "size_chart_url")
SMALL_NUMBERS = ("min_order", "max_order", "warranty_months", "shelf_life_days")
# Words a sheet may use for a choice → the stored code.
CHOICE_WORDS = {
    "condition": {"baru": "new", "new": "new", "bekas": "used", "used": "used", "second": "used"},
    "warranty_type": {"tidak ada": "", "tidak": "", "tanpa garansi": "", "-": "", "toko": "shop",
        "garansi toko": "shop", "shop": "shop", "seller": "shop", "distributor": "distributor",
        "garansi distributor": "distributor", "resmi": "manufacturer", "produsen": "manufacturer",
        "garansi resmi": "manufacturer", "garansi resmi produsen": "manufacturer", "manufacturer": "manufacturer",
        "internasional": "international", "garansi internasional": "international", "international": "international"},
    "dangerous_goods": {"tidak": "", "tidak berbahaya": "", "-": "", "baterai": "battery", "battery": "battery",
        "mengandung baterai": "battery", "cairan": "liquid", "liquid": "liquid", "mudah terbakar": "flammable",
        "flammable": "flammable", "magnet": "magnet", "mengandung magnet": "magnet"},
}
MARKETPLACE_COLUMNS = ("condition", "model_number", "min_order", "max_order", "country_of_origin", "warranty_type",
    "warranty_months", "shelf_life_days", "bpom_number", "halal_number", "sni_number", "dangerous_goods",
    "insurance_required", "showcase", "video_url", "size_chart_url", "shopee_category", "tokopedia_category")
YES = {"ya", "yes", "y", "1", "true", "wajib"}
NO = {"tidak", "no", "n", "0", "false", "-"}
KEYS = (("sku", "SKU"), ("name", "Nama produk"))


def normalise_headers(rows):
    """Rows keyed by our field names, whatever names the file used for them (first match wins). Columns named
    "Atribut: Garansi" become ``{"attributes": {"Garansi": value}}``."""
    out = []
    for row in rows:
        clean = {}
        for target, names in HEADER_ALIASES.items():
            found = next((name for name in names if name in row), None)
            if found is not None:
                clean[target] = row[found]
        extra = {}
        for header, value in row.items():
            prefix = next((p for p in ATTRIBUTE_PREFIXES if header.startswith(p)), None)
            if prefix and header[len(prefix):].strip() and (value or "").strip():
                extra[header[len(prefix):].strip().title()[:60]] = value.strip()[:200]
        if extra:
            clean["attributes"] = extra
        out.append(clean)
    return out


def _dimensions(text):
    """"30x20x12", "30 × 20 × 12 cm", "30*20*12" → (30, 20, 12); None when unreadable."""
    import re
    parts = re.split(r"\s*[x×*]\s*", re.sub(r"(?i)\s*cm\s*$", "", (text or "").strip().lower()))
    if len(parts) != 3:
        return None
    try:
        return tuple(round(float(part.replace(",", "."))) for part in parts)
    except ValueError:
        return None


def _number(text, kind):
    text = (text or "").strip()
    text = text.removesuffix(".0")          # spreadsheets write 99000.0
    if not text:
        return None
    return int(text) if kind is int else Decimal(text)


def parse_import_rows(rows, *, key="sku"):
    """Validate every row before touching the database, so a bad row fails fast.

    ``key`` is what identifies a product: its SKU (the default), or its name (to give products made by hand
    their SKU, or rename nothing and only refresh prices). A blank cell means "keep what the product has";
    a new product needs a name, a SKU and a price."""
    parsed, seen = [], {}
    for number, row in enumerate(rows, 2):
        name, sku = (row.get("name") or "").strip(), (row.get("sku") or "").strip()[:80]
        ident = sku if key == "sku" else name.lower()
        if not ident:
            raise ValueError(f"Baris {number}: {'sku' if key == 'sku' else 'name'} wajib diisi "
                f"(kunci impor: {dict(KEYS)[key]}).")
        if ident in seen:
            raise ValueError(f"Baris {number}: {'SKU ' + sku if key == 'sku' else 'nama ' + name} sudah muncul di "
                f"baris {seen[ident]}.")
        seen[ident] = number
        try:
            price = _number(row.get("price"), Decimal)
            stock = _number(row.get("stock"), int)
            weight = _number(row.get("weight_grams"), int)
            cost = _number(row.get("cost_price"), Decimal)
            dims = [_number(row.get(name), int) for name in ("length_cm", "width_cm", "height_cm")]
        except (InvalidOperation, ValueError):
            raise ValueError(f"Baris {number}: price, stock, weight_grams, atau cost_price "
                "tidak valid.") from None
        combined = (row.get("dimensions") or "").strip()
        if combined:
            parsed_dims = _dimensions(combined)
            if parsed_dims is None:
                raise ValueError(f"Baris {number}: dimensi “{combined}” tidak terbaca (pakai P x L x T, mis. 30x20x12).")
            dims = [dims[i] if dims[i] is not None else parsed_dims[i] for i in range(3)]
        numbers = [value for value in (price, stock, weight, cost, *dims) if value is not None]
        if any(not Decimal(value).is_finite() or value < 0 for value in numbers):
            raise ValueError(f"Baris {number}: angka tidak boleh negatif.")
        if (price or 0) >= 10 ** 12 or (cost or 0) >= 10 ** 12:
            raise ValueError(f"Baris {number}: price atau cost_price terlalu besar.")
        extra = {}
        for col in SMALL_NUMBERS:
            text = (row.get(col) or "").strip().removesuffix(".0")
            if text:
                if not text.isdigit() or int(text) > 32000:
                    raise ValueError(f"Baris {number}: {col} “{text}” harus angka bulat.")
                extra[col] = int(text)
        for col, words in CHOICE_WORDS.items():
            text = (row.get(col) or "").strip().lower()
            if text:
                if text not in words:
                    raise ValueError(f"Baris {number}: isi {col} “{row[col]}” tidak dikenali "
                        f"(pilihan: {', '.join(sorted({w for w in words if w != '-'}))}).")
                extra[col] = words[text] or None   # None: set back to "none" (kept apart from a blank cell)
        insured = (row.get("insurance_required") or "").strip().lower()
        if insured:
            if insured not in YES | NO:
                raise ValueError(f"Baris {number}: wajib asuransi “{insured}” harus Ya atau Tidak.")
            extra["insurance_required"] = insured in YES
        for col in URL_FIELDS:
            link = (row.get(col) or "").strip()
            if link:
                if not link.startswith(("http://", "https://")):
                    raise ValueError(f"Baris {number}: {col} harus berupa link (http…).")
                extra[col] = link[:300]
        if extra.get("min_order") is not None and extra.get("max_order") and extra["max_order"] < extra["min_order"]:
            raise ValueError(f"Baris {number}: maksimal pembelian lebih kecil dari minimal pembelian.")
        values = {"name": name[:160], "price": price, "weight_grams": weight,
            "description": (row.get("description") or "").strip(),
            "image_url": (row.get("image_url") or "").strip()[:200], "cost_price": cost,
            "length_cm": dims[0], "width_cm": dims[1], "height_cm": dims[2],
            **{field: (row.get(field) or "").strip()[:160 if field.endswith("category") else 80] for field in TEXT_FIELDS}}
        parsed.append({"number": number, "sku": sku, "key": ident, "stock": stock, "attributes": row.get("attributes") or {},
            "category": (row.get("category") or "").strip(),
            # A blank cell keeps what the product already has.
            "values": {**{field: value for field, value in values.items() if value not in (None, "")},
                **{field: ("" if value is None else value) for field, value in extra.items()}}})
    return parsed


@dataclass
class PlannedRow:
    number: int
    sku: str
    name: str
    action: str                     # "create", "update" or "error"
    product: object = None
    error: str = ""
    changes: list = field(default_factory=list)


def plan_import(store, parsed, *, key="sku"):
    """What the import would do to each row, without writing: ``[PlannedRow]``."""
    products = Product.objects.filter(store=store)
    by_sku = {p.sku: p for p in products.exclude(sku="")}
    by_name = {}
    for product in products:
        by_name.setdefault(product.name.strip().lower(), []).append(product)
    variant_skus = set(ProductVariant.objects.filter(product__store=store).exclude(sku="")
        .values_list("sku", flat=True))
    out = []
    for row in parsed:
        values, sku = row["values"], row["sku"]
        planned = PlannedRow(number=row["number"], sku=sku, name=values.get("name", ""), action="create")
        if key == "sku":
            planned.product = by_sku.get(sku)
        else:
            matches = by_name.get(row["key"], [])
            if len(matches) > 1:
                planned.action, planned.error = "error", f"{len(matches)} produk bernama “{planned.name}”; pakai kunci SKU"
            planned.product = matches[0] if len(matches) == 1 else None
        product = planned.product
        if planned.action != "error":
            if sku and sku in variant_skus:
                planned.action, planned.error = "error", f"SKU {sku} sudah dipakai varian produk"
            elif sku and key == "name" and by_sku.get(sku) not in (None, product):
                planned.action, planned.error = "error", f"SKU {sku} sudah dipakai produk {by_sku[sku].name}"
            elif product is None and not (values.get("name") and sku and "price" in values):
                planned.action, planned.error = "error", "produk baru perlu name, sku, dan price"
            elif product is not None:
                planned.action = "update"
                planned.name = planned.name or product.name
                planned.changes = [label for field, label in (("name", "nama"), ("price", "harga"),
                    ("cost_price", "harga pokok"), ("description", "deskripsi"), ("weight_grams", "berat"),
                    ("image_url", "foto"), ("brand", "merek"), ("barcode", "barcode"), ("color", "warna"),
                    ("size", "ukuran"), ("material", "bahan"), ("condition", "kondisi"), ("model_number", "model"),
                    ("min_order", "min. beli"), ("max_order", "maks. beli"), ("country_of_origin", "negara asal"),
                    ("warranty_type", "garansi"), ("warranty_months", "masa garansi"), ("shelf_life_days", "masa simpan"),
                    ("bpom_number", "BPOM"), ("halal_number", "halal"), ("sni_number", "SNI"),
                    ("dangerous_goods", "barang berbahaya"), ("insurance_required", "asuransi"), ("showcase", "etalase"),
                    ("video_url", "video"), ("size_chart_url", "tabel ukuran"), ("shopee_category", "kategori Shopee"),
                    ("tokopedia_category", "kategori Tokopedia")) if field in values and values[field] != getattr(product, field)]
                if any(name in values and values[name] != getattr(product, name)
                        for name in ("length_cm", "width_cm", "height_cm")):
                    planned.changes.append("dimensi")
                if any((product.attributes or {}).get(key) != value for key, value in row["attributes"].items()):
                    planned.changes.append("atribut")
                if key == "name" and sku and sku != product.sku:
                    planned.changes.append("SKU")
                if row["category"]:
                    planned.changes.append("kategori")
                if row["stock"] is not None:
                    planned.changes.append("stok")
        out.append(planned)
    return out


def import_summary(plans):
    return {"create": sum(p.action == "create" for p in plans), "update": sum(p.action == "update" for p in plans),
        "unchanged": sum(p.action == "update" and not p.changes for p in plans),
        "error": sum(p.action == "error" for p in plans)}


def import_products(store, parsed, *, actor=None, key="sku"):
    """Add or update products, matched by SKU (``key="sku"``) or by name, with a fixed number of queries per
    batch rather than per row. Refuses the whole file when any row cannot be applied (``plan_import``).
    The store row lock serialises this with Product.save()'s SKU uniqueness check."""
    _lock_store(store.id)
    plans = plan_import(store, parsed, key=key)
    bad = next((p for p in plans if p.action == "error"), None)
    if bad:
        raise ValueError(f"Baris {bad.number}: {bad.error}.")
    new_count = sum(p.action == "create" for p in plans)
    # Imported rows are saved active, so they all count toward the plan's product limit.
    touched = [p.product.pk for p in plans if p.product is not None]
    limit_error = product_limit_error(store, new_count + len(touched) + Product.objects.filter(
        store=store, is_active=True).exclude(pk__in=touched).count())
    if limit_error:
        raise ValueError(limit_error)
    warehouse, _ = Warehouse.objects.get_or_create(store=store, code="MAIN",
        defaults={"name": "Gudang Utama", "priority": 10})

    from apps.catalog.categories import ensure_paths
    categories = ensure_paths(store, [row["category"] for row in parsed])

    taken_slugs = set(Product.objects.filter(store=store).values_list("slug", flat=True))
    to_create, to_update, product_of = [], [], {}
    for row, planned in zip(parsed, plans, strict=True):
        values = dict(row["values"])
        if row["category"]:
            values["category"] = categories.get(row["category"])
        product = planned.product
        if product is not None:
            for name, value in values.items():
                setattr(product, name, value)
            if row["attributes"]:
                product.attributes = {**(product.attributes or {}), **row["attributes"]}
            if row["sku"]:
                product.sku = row["sku"]
            product.is_active = True
            to_update.append(product)
            product_of[row["number"]] = product
            continue
        base_slug = slugify(values["name"])[:45] or slugify(row["sku"])[:45] or "product"
        candidate, suffix = base_slug, 2
        while candidate in taken_slugs:
            candidate, suffix = f"{base_slug}-{suffix}", suffix + 1
        taken_slugs.add(candidate)
        product = Product(store=store, sku=row["sku"], slug=candidate, is_active=True, attributes=row["attributes"],
            **values)
        to_create.append(product)
    # Only the columns the file filled: fewer columns keep the batches large (SQLite caps the parameters).
    used = {name for row in parsed for name in row["values"]} | {"sku", "is_active"}
    if any(row["category"] for row in parsed):
        used.add("category")
    if any(row["attributes"] for row in parsed):
        used.add("attributes")
    if to_update:
        Product.objects.bulk_update(to_update, sorted(used), batch_size=BULK_BATCH_SIZE)
    Product.objects.bulk_create(to_create, batch_size=BULK_BATCH_SIZE)
    created_ids = dict(Product.objects.filter(store=store, sku__in=[p.sku for p in to_create])
        .values_list("sku", "id"))
    product_ids = {}
    for row, planned in zip(parsed, plans, strict=True):
        product_ids[row["number"]] = (product_of[row["number"]].pk if planned.product is not None
            else created_ids[row["sku"]])

    levels = {level.product_id: level for level in InventoryLevel.objects.select_for_update()
        .filter(warehouse=warehouse, product_id__in=product_ids.values())}
    now, new_levels, changed_levels, before = timezone.now(), [], [], {}
    actions = {p.number: p.action for p in plans}
    for row in parsed:
        product_id = product_ids[row["number"]]
        level = levels.get(product_id)
        if level is None:
            if row["stock"] is None and actions[row["number"]] == "update":
                continue                 # an existing product with a blank stock cell keeps its stock
            before[product_id] = 0
            new_levels.append(InventoryLevel(warehouse=warehouse, product_id=product_id,
                on_hand=row["stock"] or 0))
        elif row["stock"] is not None:
            before[product_id] = level.on_hand
            level.on_hand, level.updated_at = max(row["stock"], level.reserved), now
            changed_levels.append(level)
    InventoryLevel.objects.bulk_create(new_levels, batch_size=BULK_BATCH_SIZE)
    InventoryLevel.objects.bulk_update(changed_levels, ["on_hand", "updated_at"],
        batch_size=BULK_BATCH_SIZE)
    # Every stock change leaves a movement (the movement ledger must replay every level), like a
    # manual edit on Kelola stok. Before the books start that is all (Saldo awal books the stock);
    # afterwards the changes are Penyesuaian stok documents booked to Persediaan, so the import
    # cannot put the shelf and the books apart.
    from apps.accounting import stock_adjustments
    changed = [level for level in InventoryLevel.objects.filter(warehouse=warehouse, product_id__in=before)
        .select_related("product") if level.on_hand != before[level.product_id]]
    sources = {}
    if changed and stock_adjustments.books_started(store):
        for reason, sign in (("opening", 1), ("correction", -1)):
            picked = [level for level in changed if sign * (level.on_hand - before[level.product_id]) > 0]
            if picked:
                document = stock_adjustments.book_moved(store, warehouse=warehouse, reason=reason, actor=actor,
                    notes="Impor produk (CSV)", lines=[{"product": level.product,
                        "quantity": level.on_hand - before[level.product_id]} for level in picked])
                sources.update({level.pk: str(document.pk) for level in picked})
    movements = [InventoryMovement(store=store, inventory_level=level, kind="adjust",
            quantity=abs(level.on_hand - before[level.product_id]), reason="Impor produk (CSV)",
            actor=actor, previous_on_hand=before[level.product_id], new_on_hand=level.on_hand,
            previous_reserved=level.reserved, new_reserved=level.reserved,
            source_type="stock_adjustment" if level.pk in sources else "product_import",
            source_id=sources.get(level.pk, ""))
        for level in changed]
    InventoryMovement.objects.bulk_create(movements, batch_size=BULK_BATCH_SIZE)
    return len(to_create), len(to_update)



def create_store_defaults(store):
    """The main warehouse and a flat-rate shipping method a store needs before its first sale."""
    _warehouse, warehouse_created = Warehouse.objects.get_or_create(store=store, code="MAIN",
        defaults={"name": "Gudang Utama", "priority": 10, "is_active": True})
    _shipping, shipping_created = ShippingMethod.objects.get_or_create(store=store, code="reguler",
        defaults={"name": "Pengiriman Reguler", "provider": "manual", "flat_rate": 15000,
            "eta": "2–4 hari kerja", "is_active": True})
    return warehouse_created, shipping_created


@login_required(login_url="/login/")
def merchant_quick_defaults(request):
    if request.method != "POST":
        raise Http404
    store, _, _ = _setup_store(request)
    with transaction.atomic():
        warehouse_created, shipping_created = create_store_defaults(store)
        AuditLog.objects.create(store=store, actor=request.user, action="store.quick_defaults_created",
            object_type="Store", object_id=str(store.id), details={"warehouse": warehouse_created,
                "shipping": shipping_created})
    created = sum((warehouse_created, shipping_created))
    messages.success(request, f"Setup operasional siap. {created} data bawaan dibuat.")
    return redirect("merchant-settings")


@login_required(login_url="/login/")
def merchant_product_import_template(request):
    _setup_store(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="template-produk.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["name", "sku", "price", "stock", "category", "description", "weight_grams", "length_cm",
        "width_cm", "height_cm", "brand", "barcode", "color", "size", "material", "image_url", "cost_price",
        "condition", "model_number", "min_order", "max_order", "country_of_origin", "warranty_type", "warranty_months",
        "shelf_life_days", "bpom_number", "halal_number", "sni_number", "dangerous_goods", "insurance_required",
        "showcase", "video_url", "size_chart_url", "shopee_category", "tokopedia_category", "Atribut: Gaya"])
    writer.writerow(["Sepatu M 41 Hitam", "M2601-41-Black", "1198000", "10", "Sepatu", "Sepatu kets pria", "900", "32",
        "20", "12", "Patuh", "8991234567890", "Hitam", "41", "Kulit sintetis", "", "450000",
        "Baru", "M2601", "1", "5", "Indonesia", "Garansi toko", "6", "", "", "", "", "Tidak", "Tidak",
        "Sepatu Pria", "", "", "Sepatu Pria > Sneakers", "Sepatu Pria > Sneakers", "Kasual"])
    writer.writerow(["Celana Hitam L", "CLN-002", "149000", "25", "Pakaian", "", "600", "30", "25", "4", "", "", "Hitam",
        "L", "Katun", "", "90000", "Baru", "", "1", "", "Indonesia", "", "", "", "", "", "", "", "",
        "Celana", "", "", "Pakaian Pria > Celana", "", ""])
    return response


def _read_import(upload, key):
    """``(parsed, rows)`` from an uploaded catalogue; raises ValueError with the message to show."""
    if not upload:
        raise ValueError("Pilih file CSV atau Excel terlebih dahulu.")
    if upload.size > MAX_IMPORT_BYTES:
        raise ValueError("Ukuran file maksimal 10 MB.")
    from apps.shops.tabular import read_rows
    headers, rows = read_rows(upload, max_rows=MAX_IMPORT_ROWS)
    rows = normalise_headers(rows)
    found = {target for target, names in HEADER_ALIASES.items() if set(names) & set(headers)}
    need = {"sku"} if key == "sku" else {"name"}
    if not need <= found or not ({"name", "sku"} & found):
        raise ValueError(f"Kolom {'sku' if key == 'sku' else 'name'} tidak ditemukan. Gunakan template yang bisa "
            "diunduh di halaman ini (nama kolom Indonesia dan Accurate juga dikenali).")
    return parse_import_rows(rows, key=key), rows


@login_required(login_url="/login/")
def merchant_product_import(request):
    store, stores, role = _setup_store(request)
    key = (request.POST or request.GET).get("key", "sku")
    key = key if key in dict(KEYS) else "sku"
    context = {"merchant_store": store, "merchant_stores": stores, "merchant_role": role,
        "merchant_store_url": store_url(store), "keys": KEYS, "key": key}
    if request.method == "POST":
        try:
            parsed, rows = _read_import(request.FILES.get("file"), key)
            if request.POST.get("action") == "preview":
                plans = plan_import(store, parsed, key=key)
                context.update(plans=plans[:300], more=max(0, len(plans) - 300), summary=import_summary(plans))
                return render(request, "merchant/_product_import_preview.html", context)
            with transaction.atomic():
                created, updated = import_products(store, parsed, actor=request.user, key=key)
                AuditLog.objects.create(store=store, actor=request.user, action="products.csv_imported",
                    object_type="Product", object_id="bulk", details={"created": created,
                        "updated": updated, "rows": len(rows), "key": key})
            messages.success(request, f"Impor selesai: {created} produk dibuat, {updated} diperbarui.")
            return redirect("merchant-entity-list", section="products")
        except (UnicodeDecodeError, csv.Error, ValueError) as error:
            if request.POST.get("action") == "preview":
                context["error"] = str(error)
                return render(request, "merchant/_product_import_preview.html", context)
            messages.error(request, str(error))
    return render(request, "merchant/product_import.html", context)
