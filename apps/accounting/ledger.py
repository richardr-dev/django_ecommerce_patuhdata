"""The general ledger: the one place every module's money ends up.

Modules do not write journal lines themselves. They call ``post_entry`` with the document that
caused the entry (``source=("invoice", invoice.id)``), ``reverse_entry`` to cancel it, and
``register_backlog`` so the worker can post documents that were missed. The ledger knows nothing
about those modules, which is what lets a new one (POS, payroll...) plug in without touching it.
"""
import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.channels.adapters import PLATFORMS
from apps.orders.models import PaymentTransaction, Refund

from .models import EXPENSE_CATEGORIES, Account, JournalEntry, JournalLine, LedgerLock

logger = logging.getLogger(__name__)

CASH, GATEWAY, RECEIVABLE, MARKETPLACE = "1100", "1200", "1250", "1300"
INVENTORY, CONSIGNED, INPUT_VAT = "1400", "1450", "1500"
VAT, UNIDENTIFIED, PAYABLE = "2100", "2200", "2300"
CUSTOMER_CREDIT = "2400"
WITHHELD = "2150"  # PPh 21 / 23 / 4(2) held back from a payment, owed to the tax office
FIXED_ASSETS, ACCUMULATED_DEPRECIATION = "1600", "1690"
LOANS = "2500"
EQUITY, DRAWINGS, RETAINED = "3100", "3200", "3300"
YEAR_CLOSE = "year_close"  # source type of the closing entry that moves a year's profit to RETAINED
SALES, SHIPPING, REFUNDS, DISCOUNTS = "4100", "4200", "4800", "4900"
COGS, FEES = "5100", "6100"
DEPRECIATION, OTHER_INCOME, ASSET_GAIN, BANK_CHARGES = "6950", "7100", "7200", "8100"
# What a marketplace keeps from a sale, one account each so the profit and loss shows where the
# money goes. Anything the marketplace does not itemise stays in FEES (6100).
FEE_ACCOUNTS = {"commission": "6110", "service_fee": "6120", "admin_fee": "6120",
    "shipping_fee": "6130", "voucher": "6140", "affiliate": "6150", "other_fee": FEES}
# Every store gets this chart on its first posting. Refunds and discounts are contra-revenue:
# revenue accounts whose balance runs on the debit side and so reduce net sales; accumulated
# depreciation is a contra-asset the same way.
CHART = [
    (CASH, "Kas & bank", "asset"),
    (GATEWAY, "Piutang payment gateway", "asset"),
    (RECEIVABLE, "Piutang usaha", "asset"),
    (MARKETPLACE, "Piutang marketplace", "asset"),
    (INVENTORY, "Persediaan barang", "asset"),
    (CONSIGNED, "Persediaan di mitra (konsinyasi)", "asset"),
    (INPUT_VAT, "PPN masukan", "asset"),
    (FIXED_ASSETS, "Aset tetap", "asset"),
    (ACCUMULATED_DEPRECIATION, "Akumulasi penyusutan", "asset"),
    (VAT, "Utang PPN keluaran", "liability"),
    (WITHHELD, "Utang PPh dipotong (21/23/4(2))", "liability"),
    (UNIDENTIFIED, "Pembayaran belum teridentifikasi", "liability"),
    (PAYABLE, "Utang usaha", "liability"),
    (CUSTOMER_CREDIT, "Uang muka & kredit pelanggan", "liability"),
    (LOANS, "Utang bank & pinjaman", "liability"),
    (EQUITY, "Modal pemilik", "equity"),
    (DRAWINGS, "Prive (pengambilan pemilik)", "equity"),
    (RETAINED, "Laba ditahan", "equity"),
    (SALES, "Penjualan", "revenue"),
    (SHIPPING, "Pendapatan ongkos kirim", "revenue"),
    (REFUNDS, "Retur & refund penjualan", "revenue"),
    (DISCOUNTS, "Potongan penjualan", "revenue"),
    (COGS, "Harga pokok penjualan", "expense"),
    (FEES, "Biaya payment gateway & marketplace lainnya", "expense"),
    ("6110", "Komisi marketplace", "expense"),
    ("6120", "Biaya layanan & transaksi marketplace", "expense"),
    ("6130", "Selisih ongkos kirim marketplace", "expense"),
    ("6140", "Voucher & promo ditanggung penjual", "expense"),
    ("6150", "Komisi afiliasi marketplace", "expense"),
] + [(code, name, "expense") for code, name in EXPENSE_CATEGORIES] + [
    (DEPRECIATION, "Beban penyusutan", "expense"),
    (OTHER_INCOME, "Pendapatan lain-lain (bunga, dll.)", "revenue"),
    (ASSET_GAIN, "Laba (rugi) pelepasan aset", "revenue"),
    (BANK_CHARGES, "Biaya administrasi bank", "expense"),
]


def is_cash_code(code):
    """Accounts 1100–1199 hold the store's money: the main cash account and every bank,
    petty-cash or e-wallet account added after it."""
    return len(code) == 4 and "1100" <= code <= "1199"


class LedgerError(ValueError):
    pass


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def chart_of_accounts(store):
    accounts = {account.code: account for account in store.ledger_accounts.all()}
    for code, name, kind in CHART:
        if code not in accounts:
            accounts[code], _ = Account.objects.get_or_create(store=store, code=code,
                defaults={"name": name, "type": kind})
    return accounts


class LedgerLocked(LedgerError):
    """The entry falls in a period that has been closed (tutup buku)."""


def locked_through(store):
    lock = LedgerLock.objects.filter(store=store).first()
    return lock.locked_through if lock else None


def _open_date(store, date, strict):
    """``date`` if its period is open. In a closed period a strict posting is refused and an
    automatic one moves to the first open day, so closed months never change."""
    locked = locked_through(store)
    if not locked or date > locked:
        return date
    if strict:
        raise LedgerLocked(f"Pembukuan sampai {locked:%d/%m/%Y} sudah ditutup. "
            "Gunakan tanggal setelahnya atau buka kembali periodenya.")
    return locked + timedelta(days=1)


def live_entry(store, source_type, source_id):
    """The entry currently standing for a document, if any."""
    return JournalEntry.objects.filter(store=store, source_type=source_type,
        source_id=str(source_id), superseded=False).first()


def post_entry(store, *, date, memo, amounts, source, strict=False, actor=None, cost_center=None,
        **links):
    """Post a balanced entry for a business document, once.

    ``amounts`` maps account code to a signed amount: positive is a debit, negative a credit.
    ``source`` is ``(type, id)`` of the document. Posting a document that already has a live
    entry returns that entry. ``strict`` refuses a date in a closed period instead of moving it
    to the first open day: use it when a person chose the date and can choose another."""
    source_type, source_id = source[0], str(source[1])
    existing = live_entry(store, source_type, source_id)
    if existing:
        return existing
    amounts = {code: money(value) for code, value in amounts.items() if money(value)}
    if sum(amounts.values()) != 0:
        raise LedgerError(f"Unbalanced entry: {memo}")
    if not amounts:
        return None
    date = _open_date(store, date, strict)
    accounts = chart_of_accounts(store)
    unknown = set(amounts) - set(accounts)
    if unknown:
        raise LedgerError(f"Akun tidak dikenal: {', '.join(sorted(unknown))}")
    try:
        with transaction.atomic():
            entry = JournalEntry.objects.create(store=store, date=date, memo=memo[:300],
                source_type=source_type, source_id=source_id, created_by=actor,
                cost_center=cost_center, **links)
            JournalLine.objects.bulk_create(JournalLine(entry=entry, account=accounts[code],
                debit=max(value, 0), credit=max(-value, 0)) for code, value in amounts.items())
    except IntegrityError:
        existing = live_entry(store, source_type, source_id)  # posted concurrently
        if existing:
            return existing
        raise
    return entry


@transaction.atomic
def reverse_entry(entry, *, date=None, memo="", strict=False, actor=None):
    """Cancel an entry with an equal and opposite one, leaving both on the books. The document
    it stood for has no live entry afterwards and may be posted again."""
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.superseded:
        return entry.reversal
    date = _open_date(entry.store, date or entry.date, strict)
    reversal = JournalEntry.objects.create(store=entry.store, date=date,
        memo=(memo or f"Pembatalan: {entry.memo}")[:300], source_type="reversal",
        source_id=str(entry.pk), reverses=entry, created_by=actor)
    JournalLine.objects.bulk_create(JournalLine(entry=reversal, account_id=line.account_id,
        debit=line.credit, credit=line.debit) for line in entry.lines.all())
    entry.superseded = True
    entry.save(update_fields=["superseded"])
    return reversal


def reverse_source(store, source_type, source_id, **kwargs):
    """Reverse the live entry of a document. Returns the reversal, or None if it had none."""
    entry = live_entry(store, source_type, source_id)
    return reverse_entry(entry, **kwargs) if entry else None


def post_journal(store, *, date, memo, lines, actor=None, cost_center=None):
    """A manual journal (jurnal umum): opening balances, owner capital, depreciation,
    corrections. ``lines`` is a list of (account code, debit, credit)."""
    amounts = {}
    for code, debit, credit in lines:
        amounts[code] = amounts.get(code, Decimal(0)) + money(debit or 0) - money(credit or 0)
    if not any(amounts.values()):
        raise LedgerError("Jurnal kosong.")
    import uuid
    return post_entry(store, date=date, memo=memo, amounts=amounts, strict=True, actor=actor,
        source=("journal", uuid.uuid4().hex), cost_center=cost_center)


def _post(store, *, date, memo, amounts, strict=False, link=True, cost_center=None, **source):
    """Post for one of the documents the ledger links to directly (payment, refund, payout,
    expense). ``link=False`` leaves the document's direct link alone: used to book it again on
    another date, when the first entry (and its link) stays on the books, reversed."""
    (kind, document), = source.items()
    entry = post_entry(store, date=date, memo=memo, amounts=amounts, strict=strict,
        source=(kind, document.pk), cost_center=cost_center, **(source if link else {}))
    if entry is None:  # nothing to book: keep an empty entry so the worker does not retry
        entry = JournalEntry.objects.create(store=store, date=date, memo=memo[:300],
            source_type=kind, source_id=str(document.pk), **source)
    return entry


def receivable_account(provider):
    """Where money waits after a sale: the seller's bank for a transfer, otherwise with the
    gateway or marketplace until its payout."""
    return (CASH if provider == "bank_transfer" else
        MARKETPLACE if provider in PLATFORMS else GATEWAY)


def cost_of_goods(order):
    """Copy each item's current unit cost onto it once and return the order's total cost.
    Items whose product has no cost price are left out."""
    total = Decimal(0)
    for item in order.items.select_related("product", "variant"):
        if item.unit_cost is None:
            cost = item.variant.cost_price if item.variant else None
            item.unit_cost = cost if cost is not None else item.product.cost_price
            if item.unit_cost is None:
                continue
            item.save(update_fields=["unit_cost"])
        total += item.unit_cost * item.quantity
    return total


def _invoiced_on_credit(order):
    try:
        invoice = order.invoice
    except (ObjectDoesNotExist, AttributeError):
        return False
    return bool(live_entry(order.store, "invoice", invoice.pk))


def post_payment(payment, *, date=None, link=True):
    order = payment.order
    date = date or timezone.localdate(payment.updated_at)
    received = receivable_account(payment.provider)
    if not payment.reconciled:
        # Money arrived for an order that could not take it (e.g. expired): owed back until
        # finance refunds or matches it.
        return _post(order.store, date=date, link=link, payment=payment,
            memo=f"Pembayaran belum teridentifikasi #{str(order.id)[:8]} · {payment.provider}",
            amounts={received: payment.amount, UNIDENTIFIED: -payment.amount})
    if _invoiced_on_credit(order):
        # The sale was booked when its invoice was issued: this money settles the receivable.
        return _post(order.store, date=date, link=link, payment=payment,
            memo=f"Pelunasan invoice #{str(order.id)[:8]} · {payment.provider}",
            amounts={received: payment.amount, RECEIVABLE: -payment.amount})
    # Sales is the residual, so the entry balances even for orders priced by older rules.
    sales = order.total - order.shipping_cost - order.tax + order.discount
    cost = cost_of_goods(order)
    return _post(order.store, date=date, link=link, payment=payment,
        memo=f"Penjualan #{str(order.id)[:8]} · {payment.provider}",
        amounts={received: payment.amount, DISCOUNTS: order.discount, SALES: -sales,
            SHIPPING: -order.shipping_cost, VAT: -order.tax, COGS: cost, INVENTORY: -cost})


def redate_payment(payment, date):
    """Move a payment's sale to ``date`` (the marketplace order was placed earlier than it was
    imported): the old entry is reversed and the sale booked again on the right day, so the
    months, PPN and PPh follow the real date. A payment not yet posted needs nothing: it is
    posted on its own date later."""
    entry = live_entry(payment.order.store, "payment", payment.pk)
    if entry is None or entry.date == date:
        return entry
    reverse_entry(entry, memo=f"Koreksi tanggal penjualan #{str(payment.order_id)[:8]}")
    return post_payment(payment, date=date, link=False)


def post_refund(refund):
    order = refund.order
    # Reverse the PPN in proportion to the share of the order total refunded.
    vat = money(refund.amount * order.tax / order.total) if order.total else Decimal(0)
    paid_from = CASH if refund.method == "manual_transfer" else GATEWAY
    return _post(order.store, date=timezone.localdate(refund.updated_at), refund=refund,
        memo=f"Refund #{str(order.id)[:8]} · {refund.get_reason_display()}",
        amounts={REFUNDS: refund.amount - vat, VAT: vat, paid_from: -refund.amount})


def _fee_amounts(payout):
    """The payout's fees by account: the breakdown from the income report when there is one, the
    remainder (or all of it) in the general fees account. Always adds up to ``payout.fees``."""
    split = {}
    for code, value in (payout.fee_breakdown or {}).items():
        split[code] = split.get(code, Decimal(0)) + Decimal(str(value))
    rest = payout.fees - sum(split.values(), Decimal(0))
    if rest or not split:
        split[FEES] = split.get(FEES, Decimal(0)) + rest
    return {code: value for code, value in split.items() if value}


def post_payout(payout):
    """The provider paid out: its receivable becomes money in the bank, less its fees."""
    return _post(payout.store, date=payout.paid_on, payout=payout,
        memo=f"Pencairan {payout.provider} {payout.reference}",
        amounts={payout.bank_account.account.code if payout.bank_account_id else CASH: payout.net,
            **_fee_amounts(payout), receivable_account(payout.provider): -payout.gross})


def post_expense(expense):
    """Money spent from the bank on running the business. PPN masukan a PKP seller may credit
    against PPN keluaran is kept apart from the cost."""
    return _post(expense.store, date=expense.date, expense=expense, strict=True,
        cost_center=expense.cost_center,
        memo=f"Biaya: {expense.get_category_display()} · {expense.description}",
        amounts={expense.category: expense.amount + expense.withheld - expense.input_vat,
            INPUT_VAT: expense.input_vat, WITHHELD: -expense.withheld,
            (expense.bank_account.account.code if expense.bank_account_id else CASH):
                -expense.amount})


def post_pending_entries(limit=100):
    """Post succeeded payments and refunds that have no journal entry yet. Returns the count."""
    posted = 0
    payments = PaymentTransaction.objects.filter(kind="payment", status="succeeded",
        journal_entry__isnull=True).select_related("order__store").order_by("id")[:limit]
    refunds = Refund.objects.filter(status="succeeded", journal_entry__isnull=True).select_related(
        "order__store").order_by("created_at")[:limit]
    for post, source in [(post_payment, item) for item in payments] + [
            (post_refund, item) for item in refunds]:
        try:
            with transaction.atomic():
                post(source)
            posted += 1
        except IntegrityError:  # posted concurrently by another worker
            continue
        except LedgerError:  # leave it unposted for finance without blocking the rest
            logger.exception("ledger posting failed for %s %s", type(source).__name__, source.pk)
    return posted


# Modules register a function that posts their documents still missing from the ledger
# (documents older than the module's posting, or whose posting failed). Each returns a count.
_BACKLOG = []


def register_backlog(poster):
    if poster not in _BACKLOG:
        _BACKLOG.append(poster)
    return poster


def unposted(queryset, source_type, chunk=500):
    """The documents of ``queryset`` that have no live ledger entry."""
    ids = list(queryset.values_list("pk", flat=True))
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        posted = set(JournalEntry.objects.filter(source_type=source_type, superseded=False,
            source_id__in=[str(pk) for pk in batch]).values_list("source_id", flat=True))
        missing = [pk for pk in batch if str(pk) not in posted]
        yield from queryset.model.objects.filter(pk__in=missing)


def post_backlog():
    """Run every registered backlog poster. Returns how many entries were posted."""
    posted = 0
    for poster in _BACKLOG:
        try:
            posted += poster() or 0
        except Exception:  # one module's bad data must not stop the others
            logger.exception("ledger backlog failed for %s", poster.__module__)
    return posted


def post_everything():
    """Everything missing from the ledger: succeeded payments and refunds first, then what each
    module registered (invoices, bills, cash transactions...). Returns how many entries."""
    total = 0
    while posted := post_pending_entries(limit=500):
        total += posted
    return total + post_backlog()


def account_balances(store, *, start=None, end=None, exclude_closing=False):
    """Debit and credit totals per account, with the balance on each account's normal side.
    ``exclude_closing`` leaves out year-end closing entries (and their reversals), so reports
    show a closed year's profit instead of the zero the closing entry leaves behind."""
    period = Q()
    if exclude_closing:
        period &= ~(Q(lines__entry__source_type=YEAR_CLOSE)
            | Q(lines__entry__reverses__source_type=YEAR_CLOSE))
    if start:
        period &= Q(lines__entry__date__gte=start)
    if end:
        period &= Q(lines__entry__date__lte=end)
    accounts = Account.objects.filter(store=store).annotate(
        total_debit=Sum("lines__debit", filter=period, default=0),
        total_credit=Sum("lines__credit", filter=period, default=0))
    for account in accounts:
        debit_normal = account.type in {"asset", "expense"}
        account.balance = (account.total_debit - account.total_credit if debit_normal
            else account.total_credit - account.total_debit)
    return list(accounts)


def entries_for(store, sources):
    """The journal entries behind documents, with their reversals, oldest first: what a detail
    page shows so the books behind it are never a mystery. ``sources`` is ``[(type, id)]``."""
    keys = Q()
    for source_type, source_id in sources:
        keys |= Q(source_type=source_type, source_id=str(source_id))
    if not keys:
        return []
    entries = list(JournalEntry.objects.filter(keys, store=store))
    reversals = JournalEntry.objects.filter(store=store, reverses__in=entries)
    found = {entry.pk: entry for entry in [*entries, *reversals]}
    result = (JournalEntry.objects.filter(pk__in=found).prefetch_related("lines__account")
        .order_by("date", "id"))
    for entry in result:
        entry.rows = sorted(entry.lines.all(), key=lambda line: (line.credit > 0, line.account.code))
    return list(result)
