"""Saldo awal: starting the books on a day when the business already has money, stock, debts.

One balanced entry on the opening date: bank balances, receivables, stock, payables and loans on
one side, and Modal pemilik (the rest) on the other. Receivables and payables go in as totals; as
the customer pays (Terima uang / Terima pembayaran) or the supplier is paid, they come down, and
the aging pages show how much of the book balance is not itemised by invoice yet."""
from decimal import Decimal

from django.db import transaction

from .ledger import (
    CONSIGNED,
    EQUITY,
    INVENTORY,
    LOANS,
    PAYABLE,
    RECEIVABLE,
    LedgerError,
    chart_of_accounts,
    live_entry,
    money,
    post_entry,
    reverse_entry,
)
from .models import JournalEntry

SOURCE = ("opening", "balance")


def existing(store):
    """The opening entry that stands, if one was posted."""
    return live_entry(store, *SOURCE)


@transaction.atomic
def post_opening(store, *, date, banks=None, receivable=0, payable=0, loans=0,
        stock=None, actor=None):
    """``banks`` maps a BankAccount to its balance; ``stock`` is ``(shelf_value, mitra_value)``
    to book as Persediaan, or None to leave stock out. Returns the entry."""
    if existing(store):
        raise LedgerError("Saldo awal sudah dicatat. Batalkan dulu bila ingin mengubahnya.")
    chart_of_accounts(store)
    debit = {}
    for bank, amount in (banks or {}).items():
        amount = money(amount or 0)
        if amount < 0:
            raise LedgerError(f"Saldo {bank.name} tidak boleh negatif.")
        if bank.store_id != store.id:
            raise LedgerError("Rekening tidak ditemukan.")
        if amount:
            debit[bank.account.code] = debit.get(bank.account.code, Decimal(0)) + amount
    if money(receivable or 0) < 0:
        raise LedgerError("Piutang tidak boleh negatif.")
    if money(receivable or 0):
        debit[RECEIVABLE] = money(receivable)
    if stock:
        shelf, mitra = money(stock[0] or 0), money(stock[1] or 0)
        if shelf:
            debit[INVENTORY] = shelf
        if mitra:
            debit[CONSIGNED] = mitra
    credit = {}
    if money(payable or 0) < 0 or money(loans or 0) < 0:
        raise LedgerError("Utang tidak boleh negatif.")
    if money(payable or 0):
        credit[PAYABLE] = money(payable)
    if money(loans or 0):
        credit[LOANS] = money(loans)
    if not debit and not credit:
        raise LedgerError("Isi minimal satu saldo.")
    equity = sum(debit.values(), Decimal(0)) - sum(credit.values(), Decimal(0))
    amounts = dict(debit)
    for code, value in credit.items():
        amounts[code] = amounts.get(code, Decimal(0)) - value
    amounts[EQUITY] = -equity  # modal pemilik: aset dikurangi utang
    entry = post_entry(store, date=date, source=SOURCE, memo="Saldo awal pembukuan",
        amounts=amounts, strict=True, actor=actor)
    if entry is None:
        raise LedgerError("Isi minimal satu saldo.")
    return entry


@transaction.atomic
def undo_opening(store, *, actor=None):
    entry = existing(store)
    if not entry:
        return None
    return reverse_entry(entry, strict=True, actor=actor, memo="Pembatalan saldo awal pembukuan")


def breakdown(entry):
    """The lines of an opening entry as ``[(account name, debit, credit)]``."""
    return [(line.account.name, line.debit, line.credit) for line in
        JournalEntry.objects.get(pk=entry.pk).lines.select_related("account").order_by("account__code")]
