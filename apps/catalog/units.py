"""Satuan barang (units of measure). Stock is always counted in a product's base unit (``Product.base_unit``,
pcs by default). A ``ProductUnit`` is a larger unit holding ``factor`` base units ("dus" = 12 pcs). A document
line typed in that unit keeps it for the buyer or supplier to read (2 dus × 120.000) while the stock behind it
moves ``2 × 12`` pcs at ``120.000 / 12`` each. Factors are whole numbers, so stock stays in whole base units."""
from decimal import Decimal

from django.db import transaction

from .models import ProductUnit


class UnitError(ValueError):
    pass


def base_name(product):
    return (product.base_unit or "pcs").strip() or "pcs"


def units_for(product):
    """``[(name, factor)]``: the base unit first, then the larger ones."""
    rows = [(base_name(product), 1)]
    rows += [(unit.name, unit.factor) for unit in product.units.filter(is_active=True)]
    return rows


def factor(product, unit_name):
    """How many base units one ``unit_name`` holds: 1 for the base unit, a blank, or a word that is not one of
    the product's units (free text such as "set" on a service line)."""
    if product is None:
        return 1
    name = (unit_name or "").strip().lower()
    if not name or name == base_name(product).lower():
        return 1
    # A unit taken out of use still converts: an old "2 dus" line must keep meaning 24 pcs when edited.
    found = ProductUnit.objects.filter(product=product, name__iexact=name).values_list("factor", flat=True).first()
    return found or 1


def by_barcode(store, code):
    """The ``ProductUnit`` a scanned barcode names, else None."""
    code = (code or "").strip()
    if not code:
        return None
    return ProductUnit.objects.select_related("product").filter(product__store=store, barcode=code,
        is_active=True).first()


@transaction.atomic
def save_unit(product, *, name, factor, barcode="", unit=None):
    name, barcode = (name or "").strip()[:20], (barcode or "").strip()[:80]
    factor = int(factor or 0)
    if not name:
        raise UnitError("Nama satuan wajib diisi (mis. dus, pak, lusin).")
    if name.lower() == base_name(product).lower():
        raise UnitError(f"{name} sudah satuan dasar {product.name}.")
    if factor < 2:
        raise UnitError("Isi satuan harus 2 atau lebih (berapa satuan dasar dalam satu satuan ini).")
    if product.variants.filter(is_active=True).exists():
        raise UnitError(f"{product.name} punya varian; satuan untuk produk bervarian belum didukung.")
    same = ProductUnit.objects.filter(product=product, name__iexact=name)
    if unit is not None:
        same = same.exclude(pk=unit.pk)
    retired = same.filter(is_active=False).first()
    if unit is None and retired is not None:
        unit = retired   # taken out of use earlier: it comes back
        same = same.exclude(pk=retired.pk)
    if unit is not None and unit.factor != factor:
        # Old lines read the factor again when edited; changing it would move a different number of pcs.
        raise UnitError(f"Isi satuan {unit.name} ({unit.factor}) tidak bisa diubah; buat satuan dengan nama lain "
            f"(mis. {unit.name} {factor}).")
    if same.exists():
        raise UnitError(f"Satuan {name} untuk {product.name} sudah ada.")
    if barcode and ProductUnit.objects.filter(product__store=product.store, barcode=barcode).exclude(
            pk=unit.pk if unit else None).exists():
        raise UnitError(f"Barcode {barcode} sudah dipakai satuan lain.")
    if unit is None:
        return ProductUnit.objects.create(product=product, name=name, factor=factor, barcode=barcode)
    unit.name, unit.factor, unit.barcode, unit.is_active = name, factor, barcode, True
    unit.save()
    return unit


def per_base(price, unit_factor):
    """A price per ``unit_factor`` base units, per base unit (to the cent)."""
    return (Decimal(price) / Decimal(unit_factor)).quantize(Decimal("0.01")) if unit_factor > 1 else Decimal(price)
