"""PPh withheld from payments (PPh 21, 23 and 4(2)), its bukti potong, and paying it to the state.

Paying a supplier or payee for services or rent, the store holds back part of the amount for the tax
office. The payable is settled in full, the bank pays less, and the difference waits in Utang PPh
until it is deposited (setor). Each withholding gets a bukti potong number, and the month's register
is what the monthly PPh return is prepared from."""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from apps.shops.audit import log as audit_log

from .ledger import CASH, WITHHELD, LedgerError, money, post_entry
from .models import WITHHOLDING_KINDS, Withholding, WithholdingDeposit


class WithholdingError(ValueError):
    pass


KINDS = {code: {"label": label, "tax": tax, "rate": rate} for code, label, tax, rate in WITHHOLDING_KINDS}


def default_rate(kind):
    return KINDS[kind]["rate"]


def compute(kind, base, rate=None):
    """``(rate, amount)`` for a base amount. Raises WithholdingError for an unknown kind or rate."""
    if kind not in KINDS:
        raise WithholdingError("Jenis PPh tidak dikenal.")
    rate = Decimal(str(rate)) if rate not in (None, "") else KINDS[kind]["rate"]
    if not Decimal(0) < rate <= Decimal(50):
        raise WithholdingError("Tarif PPh harus lebih dari 0 dan paling banyak 50%.")
    return rate, (Decimal(base) * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def record(store, *, kind, date, base, rate, payee_name, payee_npwp="", source_type, source_id, actor=None,
        amount=None):
    """Create the withholding and its bukti potong number. Returns it. ``amount`` is given only when it is not
    rate × base: PPh 21 of December, recalculated over the year, which is negative when too much was withheld
    (lebih potong, returned to the employee and set off in the same month's deposit)."""
    from apps.tax.numbering import next_number
    if amount is None:
        rate, amount = compute(kind, base, rate)
    else:
        rate, amount = Decimal(rate or 0), Decimal(amount)
    withholding = Withholding.objects.create(store=store, kind=kind, date=date, base=base, rate=rate,
        amount=amount, payee_name=payee_name[:160], payee_npwp=(payee_npwp or "")[:20],
        number=next_number(store, "BP", date), source_type=source_type, source_id=str(source_id))
    audit_log(store, actor, "withholding.recorded", withholding, tax=withholding.tax_name, amount=amount)
    return withholding


def undeposited(store, *, until=None):
    rows = Withholding.objects.filter(store=store, deposit__isnull=True)
    return rows.filter(date__lte=until) if until else rows


@transaction.atomic
def deposit(store, ids, *, date, bank_account=None, reference="", actor=None):
    """Pay the chosen withholdings to the tax office: Utang PPh falls, the bank falls."""
    rows = list(Withholding.objects.select_for_update().filter(store=store, pk__in=ids, deposit__isnull=True))
    if not rows:
        raise WithholdingError("Pilih PPh yang belum disetor.")
    if bank_account is not None and bank_account.store_id != store.id:
        raise WithholdingError("Rekening tidak ditemukan.")
    total = sum((row.amount for row in rows), Decimal(0))
    paid = WithholdingDeposit.objects.create(store=store, date=date, amount=total, bank_account=bank_account,
        reference=reference[:60], created_by=actor)
    try:
        post_entry(store, date=date, strict=True, actor=actor, source=("pph_deposit", paid.pk),
            memo=f"Setor PPh dipotong {reference}".strip(),
            amounts={WITHHELD: money(total), (bank_account.account.code if bank_account else CASH): -money(total)})
    except LedgerError as error:
        raise WithholdingError(str(error)) from error
    Withholding.objects.filter(pk__in=[r.pk for r in rows]).update(deposit=paid)
    audit_log(store, actor, "withholding.deposited", paid, amount=total, count=len(rows))
    return paid


def month_rows(store, year, month):
    first = datetime.date(year, month, 1)
    last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    return Withholding.objects.filter(store=store, date__gte=first, date__lte=last).order_by("date", "id")
