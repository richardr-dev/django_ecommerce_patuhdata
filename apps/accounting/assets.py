"""Aset tetap: buying, depreciating and disposing of the things a business uses for years
(laptops, a delivery motorbike, an oven).

Buying one is booked once (``("asset", id)``). Depreciation is straight-line per month, one entry
per asset per month (``("depreciation", "<id>:<YYYY-MM>")``), so running it twice books nothing
twice and a missed month is caught up. The last month takes the rounding so the asset ends at
exactly its residual value."""
import calendar
from datetime import date as Date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum

from .ledger import (
    ACCUMULATED_DEPRECIATION,
    ASSET_GAIN,
    DEPRECIATION,
    FIXED_ASSETS,
    LedgerError,
    chart_of_accounts,
    live_entry,
    money,
    post_entry,
    register_backlog,
    unposted,
)
from .models import ASSET_GROUPS, FixedAsset, JournalLine

GROUP_LIFE = {code: years for code, _, years in ASSET_GROUPS}


class AssetError(ValueError):
    pass


def month_end(year, month):
    return Date(year, month, calendar.monthrange(year, month)[1])


def _months(start, end):
    """(year, month) from ``start``'s month through ``end``'s month."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            year, month = year + 1, 1


def monthly_charge(asset):
    return money((asset.cost - asset.residual_value) / asset.useful_life_months)


def accumulated(asset, *, through=None):
    lines = JournalLine.objects.filter(entry__store=asset.store, account__code=ACCUMULATED_DEPRECIATION,
        entry__source_type="depreciation", entry__source_id__startswith=f"{asset.pk}:",
        entry__superseded=False)
    if through:
        lines = lines.filter(entry__date__lte=through)
    totals = lines.aggregate(c=Sum("credit", default=0), d=Sum("debit", default=0))
    return totals["c"] - totals["d"]


def book_value(asset):
    return asset.cost - accumulated(asset)


def schedule(asset):
    """Every month of the asset's life: charge, accumulated and book value after it."""
    rows, total = [], Decimal(0)
    charge = monthly_charge(asset)
    depreciable = asset.cost - asset.residual_value
    for index, (year, month) in enumerate(_months(asset.acquired_on, Date(9999, 12, 31))):
        if index >= asset.useful_life_months:
            break
        amount = depreciable - total if index == asset.useful_life_months - 1 else charge
        total += amount
        rows.append({"month": Date(year, month, 1), "amount": amount, "accumulated": total,
            "book_value": asset.cost - total})
    return rows


@transaction.atomic
def acquire(store, *, name, acquired_on, cost, useful_life_months, paid_from_code,
        residual_value=0, group="x", number="", notes="", actor=None):
    cost, residual_value = money(cost or 0), money(residual_value or 0)
    if not name.strip():
        raise AssetError("Nama aset wajib diisi.")
    if cost <= 0:
        raise AssetError("Harga perolehan harus lebih dari nol.")
    if residual_value < 0 or residual_value >= cost:
        raise AssetError("Nilai sisa harus di bawah harga perolehan.")
    if not useful_life_months or useful_life_months <= 0:
        raise AssetError("Masa manfaat wajib diisi.")
    accounts = chart_of_accounts(store)
    if paid_from_code not in accounts:
        raise AssetError("Pilih akun sumber dana.")
    asset = FixedAsset.objects.create(store=store, name=name.strip()[:160], number=number[:40],
        group=group, acquired_on=acquired_on, cost=cost, residual_value=residual_value,
        useful_life_months=useful_life_months, paid_from=accounts[paid_from_code],
        notes=notes, created_by=actor)
    post_acquisition(asset, strict=True, actor=actor)
    return asset


def post_acquisition(asset, *, strict=False, actor=None):
    return post_entry(asset.store, date=asset.acquired_on, source=("asset", asset.pk),
        memo=f"Pembelian aset tetap: {asset.name}", strict=strict, actor=actor,
        amounts={FIXED_ASSETS: asset.cost, asset.paid_from.code: -asset.cost})


def depreciate(store, *, through, actor=None):
    """Book every month of depreciation due up to ``through``. Returns the entries posted."""
    posted = []
    for asset in FixedAsset.objects.filter(store=store, status=FixedAsset.ACTIVE,
            acquired_on__lte=through):
        posted += _depreciate_asset(asset, through=through, actor=actor)
    return posted


def _depreciate_asset(asset, *, through, actor=None):
    posted = []
    plan = schedule(asset)
    for row in plan:
        end = month_end(row["month"].year, row["month"].month)
        if end > through:
            break
        key = f"{asset.pk}:{row['month']:%Y-%m}"
        if live_entry(asset.store, "depreciation", key):
            continue
        try:
            with transaction.atomic():
                entry = post_entry(asset.store, date=end, source=("depreciation", key),
                    memo=f"Penyusutan {asset.name} · {row['month']:%m/%Y}", actor=actor,
                    amounts={DEPRECIATION: row["amount"], ACCUMULATED_DEPRECIATION: -row["amount"]})
        except (IntegrityError, LedgerError):
            continue
        if entry:
            posted.append(entry)
    return posted


@transaction.atomic
def dispose(asset, *, disposed_on, proceeds=0, received_into_code=None, actor=None):
    """Sell or write off an asset: depreciation runs through the month before, then cost and
    accumulated depreciation leave the books and the difference to what was received is a gain
    or loss."""
    asset = FixedAsset.objects.select_for_update().get(pk=asset.pk)
    if asset.status != FixedAsset.ACTIVE:
        raise AssetError("Aset ini sudah dilepas.")
    if disposed_on < asset.acquired_on:
        raise AssetError("Tanggal pelepasan sebelum tanggal perolehan.")
    proceeds = money(proceeds or 0)
    accounts = chart_of_accounts(asset.store)
    if proceeds and received_into_code not in accounts:
        raise AssetError("Pilih rekening penerima hasil penjualan.")
    first_of_month = disposed_on.replace(day=1)
    if first_of_month > asset.acquired_on.replace(day=1):
        from datetime import timedelta
        _depreciate_asset(asset, through=first_of_month - timedelta(days=1), actor=actor)
    acc = accumulated(asset)
    gain = proceeds - (asset.cost - acc)
    amounts = {FIXED_ASSETS: -asset.cost, ACCUMULATED_DEPRECIATION: acc, ASSET_GAIN: -gain}
    if proceeds:
        amounts[received_into_code] = amounts.get(received_into_code, Decimal(0)) + proceeds
    post_entry(asset.store, date=disposed_on, source=("asset_disposal", asset.pk), strict=True,
        memo=f"Pelepasan aset tetap: {asset.name}", amounts=amounts, actor=actor)
    asset.status = FixedAsset.DISPOSED
    asset.disposed_on, asset.disposal_amount = disposed_on, proceeds
    asset.disposal_to = accounts.get(received_into_code) if proceeds else None
    asset.save(update_fields=["status", "disposed_on", "disposal_amount", "disposal_to"])
    return asset, gain


@register_backlog
def post_asset_backlog(limit=200):
    posted = 0
    for asset in unposted(FixedAsset.objects.select_related("store", "paid_from")[:limit], "asset"):
        try:
            with transaction.atomic():
                post_acquisition(asset)
            posted += 1
        except (IntegrityError, LedgerError):
            continue
    return posted
