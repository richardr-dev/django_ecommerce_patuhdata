"""The partner edits the subscription plans and their prices from /platform/plans/. A price change reaches
bills made from now on; a bill already issued keeps the amount it was issued with."""
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import ProtectedError

from apps.shops import platform_log

from .models import Plan

FIELDS = ("name", "tagline", "price_monthly", "list_price_monthly", "price_yearly", "list_price_yearly", "max_products", "max_channels", "included_outlets",
    "extra_outlet_price", "allow_custom_domain", "allow_payout_reconciliation", "contact_sales", "is_public",
    "is_default", "sort_order", "features")


MONEY_FIELDS = ("price_monthly", "list_price_monthly", "price_yearly", "list_price_yearly", "extra_outlet_price")


def snapshot(plan):
    values = {}
    for field in FIELDS:
        value = getattr(plan, field)
        values[field] = "" if value is None else (int(value) if field in MONEY_FIELDS else value)
    return values


CODE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")


class PlanError(Exception):
    """A refusal the page shows as a message."""


def _money(raw, label):
    raw = "".join(ch for ch in str(raw or "0") if ch.isdigit())
    try:
        value = Decimal(raw or "0")
    except InvalidOperation:
        raise PlanError(f"{label} harus berupa angka.") from None
    return value


def _limit(raw, label):
    """A whole number, or None (blank) for no limit."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise PlanError(f"{label}: isi angka, atau kosongkan untuk tanpa batas.")
    return int(raw)


@transaction.atomic
def save_plan(plan, data, actor=None):
    """Create (``plan`` None) or update a plan from a form's values."""
    creating = plan is None
    before = {} if creating else snapshot(plan)
    name = (data.get("name") or "").strip()
    if not name:
        raise PlanError("Tulis nama paket.")
    if creating:
        code = (data.get("code") or "").strip().lower()
        if not CODE.fullmatch(code):
            raise PlanError("Kode paket: 3–40 huruf kecil, angka, atau tanda hubung.")
        if Plan.objects.filter(code=code).exists():
            raise PlanError(f"Kode paket “{code}” sudah dipakai.")
        plan = Plan(code=code)
    plan.name = name[:60]
    plan.tagline = (data.get("tagline") or "").strip()[:160]
    plan.price_monthly = _money(data.get("price_monthly"), "Harga bulanan")
    plan.price_yearly = _money(data.get("price_yearly"), "Harga tahunan")
    plan.list_price_monthly = _money(data.get("list_price_monthly"), "Harga normal bulanan")
    plan.list_price_yearly = _money(data.get("list_price_yearly"), "Harga normal tahunan")
    for label, final, slashed in (("bulanan", plan.price_monthly, plan.list_price_monthly),
            ("tahunan", plan.price_yearly, plan.list_price_yearly)):
        if slashed and slashed <= final:
            raise PlanError(f"Harga normal {label} (dicoret) harus lebih besar dari harga yang ditagih, "
                "atau isi 0 bila tanpa diskon.")
    plan.max_products = _limit(data.get("max_products"), "Produk aktif")
    plan.max_channels = _limit(data.get("max_channels"), "Toko marketplace")
    plan.included_outlets = _limit(data.get("included_outlets"), "Outlet termasuk")
    extra = str(data.get("extra_outlet_price") or "").strip()
    plan.extra_outlet_price = _money(extra, "Harga outlet tambahan") if extra else None
    plan.features = (data.get("features") or "").strip()
    plan.allow_custom_domain = bool(data.get("allow_custom_domain"))
    plan.allow_payout_reconciliation = bool(data.get("allow_payout_reconciliation"))
    plan.contact_sales = bool(data.get("contact_sales"))
    plan.is_public = bool(data.get("is_public"))
    plan.is_default = bool(data.get("is_default"))
    plan.sort_order = int(str(data.get("sort_order") or "0").strip() or 0) if str(
        data.get("sort_order") or "0").strip().lstrip("-").isdigit() else 0
    if plan.is_default and plan.contact_sales:
        raise PlanError("Paket bawaan tidak boleh paket khusus (hubungi sales).")
    if plan.is_paid and not plan.contact_sales and plan.price_yearly == 0:
        raise PlanError("Isi harga tahunan, atau 0 hanya untuk paket gratis atau paket khusus.")
    if plan.is_default:
        Plan.objects.exclude(pk=plan.pk).filter(is_default=True).update(is_default=False)
    plan.save()
    after = snapshot(plan)
    platform_log.log(actor, "plan.created" if creating else "plan.updated", "Plan", f"{plan.name} ({plan.code})",
        after if creating else platform_log.diff(before, after))
    return plan


def delete_plan(plan, actor=None):
    """Only a plan nobody has ever used can go; otherwise hide it from the pricing page."""
    if plan.is_default:
        raise PlanError("Paket bawaan tidak bisa dihapus. Jadikan paket lain sebagai bawaan dulu.")
    name = plan.name
    try:
        with transaction.atomic():
            plan.delete()
    except ProtectedError:
        raise PlanError(f"Paket {name} sudah dipakai perusahaan atau tagihan. Sembunyikan saja "
            "(hilangkan centang “Tampil di halaman harga”).") from None
    platform_log.log(actor, "plan.deleted", "Plan", name)
    return name
