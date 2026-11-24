"""Saldo awal utang per tagihan: supplier bills from the old system that are still unpaid.

The mirror of ``tax/opening_invoices.py``. An opening bill is an ordinary ``Bill`` with
``is_opening``: it is paid (one bill or several at once), aged in Umur utang and listed on the supplier
like any other. ``payables.post_bill`` books only Modal pemilik / Utang usaha on the Saldo awal date
(else the bill's own date): the purchase, its stock and its PPN masukan were booked in the old books.
It has one line with no product, takes no stock, and refuses a debit note; a wrong amount is voided
and entered again. The payable total typed on the Saldo awal form is for what is not entered here."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from . import opening_paste
from .models import Bill, Supplier
from .payables import PayableError, create_bill

LINE_TEXT = "Saldo awal utang"


class OpeningBillError(PayableError):
    pass


def opening_bills(store):
    return (Bill.objects.filter(store=store, is_opening=True).select_related("supplier")
        .prefetch_related("payments", "debit_notes").order_by("bill_date", "number"))


def totals(store):
    """``(count, original total, still owed)`` of the live opening bills."""
    rows = [bill for bill in opening_bills(store) if bill.status != Bill.STATUS_VOID]
    return len(rows), sum((b.total for b in rows), Decimal(0)), sum((b.amount_due for b in rows), Decimal(0))


def books_start(store):
    from .opening import existing
    entry = existing(store)
    return entry.date if entry else None


def find_supplier(store, name):
    return Supplier.objects.filter(store=store, name__iexact=(name or "").strip()).first()


@transaction.atomic
def create_opening_bill(store, *, supplier, number, bill_date, amount, due_date=None, notes="", actor=None):
    """Carry one unpaid supplier bill over. ``amount`` is what is still owed on it today."""
    if supplier is None or supplier.store_id != store.id:
        raise OpeningBillError("Pilih supplier.")
    number = (number or "").strip()[:40]
    if not number:
        raise OpeningBillError("Isi nomor tagihan dari supplier.")
    if Bill.objects.filter(store=store, supplier=supplier, number=number).exclude(status=Bill.STATUS_VOID).exists():
        raise OpeningBillError(f"Tagihan {number} dari {supplier.name} sudah dicatat.")
    amount = Decimal(amount or 0)
    if amount <= 0:
        raise OpeningBillError("Sisa tagihan harus lebih dari nol.")
    if bill_date is None:
        raise OpeningBillError("Isi tanggal tagihan.")
    start = books_start(store)
    if start and bill_date > start:
        raise OpeningBillError(f"Tagihan bertanggal setelah saldo awal ({start:%d/%m/%Y}) adalah pembelian "
            "baru: catat lewat Tagihan supplier.")
    if due_date and due_date < bill_date:
        raise OpeningBillError("Jatuh tempo tidak boleh sebelum tanggal tagihan.")
    bill = create_bill(store, supplier=supplier, number=number, bill_date=bill_date, due_date=due_date,
        lines=[{"description": LINE_TEXT, "qty": Decimal(1), "unit_price": amount}], notes=notes,
        actor=actor, opening=True)
    if due_date and due_date < _today():
        bill.status = Bill.STATUS_OVERDUE
        bill.save(update_fields=["status"])
    from apps.shops import audit
    audit.log(store, actor, "bill.opening_created", bill, amount=str(amount))
    return bill


def _today():
    from django.utils import timezone
    return timezone.localdate()


def parse(text):
    return opening_paste.parse(text, party="supplier", document="tagihan")


@transaction.atomic
def import_rows(store, rows, *, actor=None):
    """Create every row or none; an unknown supplier is added by name."""
    bad = [row for row in rows if row.error]
    if bad:
        raise OpeningBillError(f"Baris {bad[0].line}: {bad[0].error}.")
    if not rows:
        raise OpeningBillError("Tidak ada baris untuk diimpor.")
    created = []
    for row in rows:
        supplier = find_supplier(store, row.party) or Supplier.objects.create(store=store, name=row.party[:120])
        try:
            created.append(create_opening_bill(store, supplier=supplier, number=row.number,
                bill_date=row.issued_on, due_date=row.due_date, amount=row.amount, actor=actor))
        except PayableError as error:
            raise OpeningBillError(f"Baris {row.line}: {error}") from error
    return created


def payable_in_opening_entry(store):
    """Utang usaha typed as a total on the Saldo awal form (not itemised)."""
    from .ledger import PAYABLE
    from .opening import existing
    entry = existing(store)
    if not entry:
        return Decimal(0)
    return entry.lines.filter(account__code=PAYABLE).aggregate(total=Sum("credit", default=0))["total"]
