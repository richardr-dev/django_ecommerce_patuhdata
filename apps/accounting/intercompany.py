"""Transfer antar brand: goods sold between two stores of one Organization.

Each store is its own legal entity with its own ledger, so one transfer writes two entries, one in
each book, and moves the stock out of one store and into the other, all in one transaction:

  seller: Piutang antar perusahaan / Penjualan antar perusahaan, and HPP / Persediaan at its cost
  buyer:  Persediaan / Utang antar perusahaan, at the transfer price

A move between two warehouses of the *same* store is not this: it has no sale and no HPP
(``orders.stock.transfer_stock``). Settling the balance is an ordinary cash entry on each side.
PPN on an intercompany sale is not applied here. The group's receivable and payable must offset:
``controls.intercompany_tie`` checks it."""
from decimal import Decimal

from django.db import transaction

from .ledger import (
    COGS,
    INTERCO_PAYABLE,
    INTERCO_RECEIVABLE,
    INTERCO_SALES,
    INVENTORY,
    LedgerError,
    account_balances,
    post_entry,
    register_backlog,
)
from .models import IntercompanyLine, IntercompanyTransfer
from .payables import _find_goods


class TransferError(ValueError):
    pass


def counterparts(store):
    """The other stores of this store's Organization."""
    from apps.shops.models import Store
    return Store.objects.filter(organization_id=store.organization_id).exclude(pk=store.pk).order_by("name")


def post_transfer(transfer, *, strict=False, actor=None):
    """Both entries, one per book. Safe to repeat: each is posted once per store."""
    sale = post_entry(transfer.from_store, date=transfer.transfer_date, strict=strict, actor=actor,
        source=("ic_out", transfer.pk), memo=f"Penjualan ke {transfer.to_store.name} {transfer.number}".strip(),
        amounts={INTERCO_RECEIVABLE: transfer.price_total, INTERCO_SALES: -transfer.price_total,
            COGS: transfer.cost_total, INVENTORY: -transfer.cost_total})
    purchase = post_entry(transfer.to_store, date=transfer.transfer_date, strict=strict, actor=actor,
        source=("ic_in", transfer.pk), memo=f"Pembelian dari {transfer.from_store.name} {transfer.number}".strip(),
        amounts={INVENTORY: transfer.price_total, INTERCO_PAYABLE: -transfer.price_total})
    return sale, purchase


@transaction.atomic
def create_transfer(from_store, to_store, *, lines, transfer_date, number="", notes="", actor=None):
    """``lines`` is a list of dicts: sku, qty (whole units), unit_price (the transfer price)."""
    from apps.orders.stock import StockError, receive_goods, stock_out

    if from_store.pk == to_store.pk:
        raise TransferError("Pilih brand tujuan yang berbeda.")
    if not from_store.organization_id or from_store.organization_id != to_store.organization_id:
        raise TransferError("Transfer hanya antar brand dalam satu organisasi.")
    if not lines:
        raise TransferError("Tambahkan minimal satu barang.")
    resolved = []
    for line in lines:
        sku = (line.get("sku") or "").strip()
        source_product, source_variant = _find_goods(from_store, sku)
        target_product, target_variant = _find_goods(to_store, sku)
        if not source_product:
            raise TransferError(f"SKU {sku or '(kosong)'} tidak ada di {from_store.name}.")
        if not target_product:
            raise TransferError(f"SKU {sku} belum ada di katalog {to_store.name}. Tambahkan produknya dulu.")
        qty, price = line["qty"], line["unit_price"]
        if qty != int(qty) or qty <= 0:
            raise TransferError(f"Jumlah {source_product.name} harus bilangan bulat lebih dari nol.")
        if price < 0:
            raise TransferError("Harga transfer tidak boleh negatif.")
        holder = source_variant or source_product
        cost = Decimal(getattr(holder, "cost_price", None) or source_product.cost_price or 0)
        resolved.append((source_product, source_variant, target_product, target_variant, sku, int(qty), cost, price))
    transfer = IntercompanyTransfer.objects.create(organization_id=from_store.organization_id,
        from_store=from_store, to_store=to_store, number=number, transfer_date=transfer_date, notes=notes,
        created_by=actor)
    IntercompanyLine.objects.bulk_create(IntercompanyLine(transfer=transfer, from_product=sp, to_product=tp,
        sku=sku, description=sp.name, quantity=qty, unit_cost=cost, unit_price=price)
        for sp, _sv, tp, _tv, sku, qty, cost, price in resolved)
    try:
        for sp, sv, tp, tv, _sku, qty, cost, price in resolved:
            stock_out(store=from_store, product=sp, variant=sv, quantity=qty, kind="transfer_out",
                reason=f"Dijual ke {to_store.name} {number}".strip(), source=("ic_transfer", str(transfer.pk)),
                unit_cost=cost, actor=actor)
            receive_goods(store=to_store, product=tp, variant=tv, quantity=qty, unit_cost=price, actor=actor,
                reason=f"Dibeli dari {from_store.name} {number}".strip(), source=("ic_transfer", str(transfer.pk)))
        post_transfer(transfer, strict=True, actor=actor)
    except (StockError, LedgerError) as error:
        raise TransferError(str(error)) from error
    return transfer


def group_balances(organization):
    """Receivable and payable between the Organization's stores, as each book shows them."""
    from apps.shops.models import Store
    receivable = payable = Decimal(0)
    for store in Store.objects.filter(organization=organization):
        balances = {a.code: a.balance for a in account_balances(store)}
        receivable += balances.get(INTERCO_RECEIVABLE, Decimal(0))
        payable += balances.get(INTERCO_PAYABLE, Decimal(0))
    return receivable, payable


@register_backlog
def post_unposted_transfers():
    """A transfer missing either book's entry (a failed posting) is completed by the worker."""
    from .models import JournalEntry
    live = JournalEntry.objects.filter(superseded=False)
    sold = set(live.filter(source_type="ic_out").values_list("source_id", flat=True))
    bought = set(live.filter(source_type="ic_in").values_list("source_id", flat=True))
    posted = 0
    for transfer in IntercompanyTransfer.objects.select_related("from_store", "to_store"):
        if str(transfer.pk) not in sold or str(transfer.pk) not in bought:
            posted += bool(post_transfer(transfer))
    return posted


__all__ = ["TransferError", "counterparts", "create_transfer", "group_balances", "post_transfer"]
