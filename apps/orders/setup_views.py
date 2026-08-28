import csv
import io
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.text import slugify

from apps.catalog.models import Category, InventoryLevel, Product, Warehouse
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
        elif upload.size > 2 * 1024 * 1024:
            messages.error(request, "Ukuran CSV maksimal 2 MB.")
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
                if len(rows) > 1000:
                    raise ValueError("Maksimal 1.000 produk per impor.")
                with transaction.atomic():
                    warehouse, _ = Warehouse.objects.get_or_create(store=store, code="MAIN",
                        defaults={"name": "Gudang Utama", "priority": 10})
                    created = updated = 0
                    for number, row in enumerate(rows, 2):
                        name, sku = row.get("name", "").strip(), row.get("sku", "").strip()
                        if not name or not sku:
                            raise ValueError(f"Baris {number}: name dan sku wajib diisi.")
                        try:
                            price = Decimal(row["price"].strip())
                            stock = int(row["stock"].strip())
                            weight = int((row.get("weight_grams") or "0").strip())
                        except (InvalidOperation, ValueError):
                            raise ValueError(f"Baris {number}: price, stock, atau weight_grams tidak valid.") from None
                        if price < 0 or stock < 0 or weight < 0:
                            raise ValueError(f"Baris {number}: angka tidak boleh negatif.")
                        category_name = (row.get("category") or "").strip()
                        category = None
                        if category_name:
                            category_slug = slugify(category_name)[:50] or "category"
                            category, _ = Category.objects.get_or_create(store=store, slug=category_slug,
                                defaults={"name": category_name[:100]})
                        product = Product.objects.filter(store=store, sku=sku).first()
                        values = {"name": name[:160], "price": price, "category": category,
                            "description": (row.get("description") or "").strip(), "weight_grams": weight,
                            "image_url": (row.get("image_url") or "").strip(), "is_active": True}
                        if product:
                            for field, value in values.items(): setattr(product, field, value)
                            product.save()
                            updated += 1
                        else:
                            base_slug = slugify(name)[:45] or slugify(sku)[:45] or "product"
                            candidate, suffix = base_slug, 2
                            while Product.objects.filter(store=store, slug=candidate).exists():
                                candidate, suffix = f"{base_slug}-{suffix}", suffix + 1
                            product = Product.objects.create(store=store, sku=sku[:80], slug=candidate, **values)
                            created += 1
                        level, _ = InventoryLevel.objects.get_or_create(warehouse=warehouse, product=product)
                        level.on_hand = max(stock, level.reserved)
                        level.save(update_fields=["on_hand", "updated_at"])
                    AuditLog.objects.create(store=store, actor=request.user, action="products.csv_imported",
                        object_type="Product", object_id="bulk", details={"created": created,
                            "updated": updated, "rows": len(rows)})
                messages.success(request, f"Impor selesai: {created} produk dibuat, {updated} diperbarui.")
                return redirect("merchant-entity-list", section="products")
            except (UnicodeDecodeError, csv.Error, ValueError) as error:
                messages.error(request, str(error))
    context = {"merchant_store": store, "merchant_stores": stores, "merchant_role": role}
    return render(request, "merchant/product_import.html", context)
