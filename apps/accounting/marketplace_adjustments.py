"""Selisih & klaim marketplace: money between the company and a marketplace that no imported payout carries.

Recording it moves Piutang marketplace (1300) against the account chosen:
- a charge (extra ongkir Shopee takes back, a penalty): debit the account (6130, 4200, 1290...), credit 1300;
- a claim (compensation, a shipping overcharge to be refunded): debit 1300, credit the account.
Settling it is a second entry between 1300 and the bank: a charge is settled when the marketplace keeps it out of
a payout or the company transfers it (debit 1300, credit bank); a claim when the money arrives (debit bank, credit
1300). An open adjustment is part of what the marketplace still owes, so `controls.held_by_providers` ties.
Neither entry is edited: voiding or unsettling reverses it."""
import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.shops import audit

from .ledger import (
    CASH,
    LedgerError,
    live_entry,
    post_entry,
    receivable_account,
    register_backlog,
    reverse_source,
)
from .models import Account, BankAccount, MarketplaceAdjustment
from .payouts import payout_providers

SOURCE, SETTLE = "mp_adjustment", "mp_adjustment_settle"
# The usual counter account for each kind, preselected on the form.
DEFAULT_ACCOUNT = {MarketplaceAdjustment.CHARGE: "6130", MarketplaceAdjustment.CLAIM: "7100"}


class AdjustmentError(ValueError):
    pass


def counter_accounts(store):
    """Accounts that may sit on the other side: anything but money accounts and the provider receivables."""
    banks = set(BankAccount.objects.filter(store=store).values_list("account_id", flat=True))
    return [a for a in Account.objects.filter(store=store).order_by("code")
        if a.pk not in banks and a.code not in {CASH, "1200", "1300"}]


def _amount(value):
    try:
        amount = Decimal(str(value).replace(".", "").replace(",", ".") if isinstance(value, str) else value)
    except (InvalidOperation, ValueError):
        raise AdjustmentError("Jumlah tidak valid.") from None
    if amount <= 0:
        raise AdjustmentError("Jumlah harus lebih dari nol.")
    return amount


def post_adjustment(adj, *, strict=False, actor=None):
    held = receivable_account(adj.provider)
    amounts = ({adj.account.code: adj.amount, held: -adj.amount} if adj.kind == adj.CHARGE
        else {held: adj.amount, adj.account.code: -adj.amount})
    return post_entry(adj.store, date=adj.date, strict=strict, actor=actor, source=(SOURCE, adj.pk),
        memo=f"Selisih marketplace {adj.provider} · {adj.description}", amounts=amounts)


def _post_settlement(adj, *, strict=True, actor=None):
    held = receivable_account(adj.provider)
    bank = adj.settled_bank.account.code if adj.settled_bank_id else CASH
    amounts = {held: adj.amount, bank: -adj.amount} if adj.kind == adj.CHARGE else {bank: adj.amount, held: -adj.amount}
    return post_entry(adj.store, date=adj.settled_on, strict=strict, actor=actor, source=(SETTLE, adj.pk),
        memo=f"Pelunasan selisih marketplace {adj.provider} · {adj.description}", amounts=amounts)


@transaction.atomic
def record(store, *, provider, kind, account, amount, date, description, reference="", actor=None):
    if provider not in dict(payout_providers()):
        raise AdjustmentError("Pilih marketplace atau gateway.")
    if kind not in dict(MarketplaceAdjustment.KINDS):
        raise AdjustmentError("Pilih jenis selisih.")
    if account is None or account.store_id != store.pk or account not in counter_accounts(store):
        raise AdjustmentError("Pilih akun lawan.")
    if not (description or "").strip():
        raise AdjustmentError("Tulis keterangan selisihnya.")
    if not isinstance(date, datetime.date):
        raise AdjustmentError("Isi tanggal.")
    adj = MarketplaceAdjustment.objects.create(store=store, provider=provider, kind=kind, account=account,
        amount=_amount(amount), date=date, description=description.strip()[:300], reference=reference[:120],
        created_by=actor)
    try:
        post_adjustment(adj, strict=True, actor=actor)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    audit.log(store, actor, "marketplace_adjustment.created", adj, amount=adj.amount, kind=kind)
    return adj


@transaction.atomic
def settle(adj, *, date, bank_account=None, note="", actor=None):
    adj = MarketplaceAdjustment.objects.select_for_update().get(pk=adj.pk)
    if not adj.is_open:
        raise AdjustmentError("Selisih ini sudah lunas atau dibatalkan.")
    if date < adj.date:
        raise AdjustmentError("Tanggal pelunasan tidak boleh sebelum tanggal selisih.")
    adj.settled_on, adj.settled_bank, adj.settled_note = date, bank_account, note[:200]
    adj.save(update_fields=["settled_on", "settled_bank", "settled_note"])
    try:
        _post_settlement(adj, actor=actor)
    except LedgerError as error:
        raise AdjustmentError(str(error)) from error
    audit.log(adj.store, actor, "marketplace_adjustment.settled", adj, amount=adj.amount)
    return adj


@transaction.atomic
def unsettle(adj, *, actor=None):
    adj = MarketplaceAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.voided or adj.settled_on is None:
        raise AdjustmentError("Selisih ini belum dilunasi.")
    reverse_source(adj.store, SETTLE, adj.pk, actor=actor, memo="Pelunasan selisih dibatalkan")
    adj.settled_on, adj.settled_bank, adj.settled_note = None, None, ""
    adj.save(update_fields=["settled_on", "settled_bank", "settled_note"])
    audit.log(adj.store, actor, "marketplace_adjustment.unsettled", adj)
    return adj


@transaction.atomic
def void(adj, *, actor=None):
    adj = MarketplaceAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.voided:
        return adj
    if adj.settled_on is not None:
        raise AdjustmentError("Batalkan pelunasannya lebih dulu.")
    reverse_source(adj.store, SOURCE, adj.pk, actor=actor, memo="Selisih marketplace dibatalkan")
    adj.voided = True
    adj.save(update_fields=["voided"])
    audit.log(adj.store, actor, "marketplace_adjustment.voided", adj)
    return adj


def open_total(store):
    """What open adjustments add to what the marketplaces owe (claims +, charges -)."""
    return sum((a.signed for a in MarketplaceAdjustment.objects.filter(store=store, voided=False,
        settled_on__isnull=True)), Decimal(0))


@register_backlog
def post_unposted_adjustments(limit=200):
    posted = 0
    for adj in MarketplaceAdjustment.objects.filter(voided=False).select_related("store", "account")[:limit]:
        try:
            if live_entry(adj.store, SOURCE, adj.pk) is None:
                posted += bool(post_adjustment(adj))
            if adj.settled_on and live_entry(adj.store, SETTLE, adj.pk) is None:
                posted += bool(_post_settlement(adj, strict=False))
        except LedgerError:
            continue
    return posted
