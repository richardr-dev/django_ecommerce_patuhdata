import csv
import io
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Min, Q, Sum

from apps.channels.adapters import PLATFORMS
from apps.channels.services import parse_amount
from apps.orders.models import PaymentTransaction

from .ledger import post_payout
from .models import Payout, PayoutLine

PAYOUT_COLUMNS = ["payout_reference", "payout_date", "order_reference", "amount", "fee"]


class PayoutImportError(ValueError):
    pass


def payout_providers():
    """Providers that hold a seller's money until a payout: their gateways and marketplaces."""
    return [("doku", "DOKU"), ("xendit", "Xendit")] + [
        (code, platform.label) for code, platform in PLATFORMS.items()]


def _date(text):
    text = (text or "").strip()
    for layout in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, layout).date()  # noqa: DTZ007 (date only)
        except ValueError:
            continue
    raise PayoutImportError(f"Tanggal tidak valid: {text or '(kosong)'} (pakai YYYY-MM-DD).")


def parse_payouts_csv(upload):
    """Group settlement rows (one per order) by payout reference."""
    text = upload.read().decode("utf-8-sig") if hasattr(upload, "read") else upload
    rows = csv.DictReader(io.StringIO(text))
    rows.fieldnames = [name.strip().lower() for name in rows.fieldnames or []]
    missing = set(PAYOUT_COLUMNS) - {"fee"} - set(rows.fieldnames)
    if missing:
        raise PayoutImportError(f"Kolom wajib tidak ada: {', '.join(sorted(missing))}")
    grouped = OrderedDict()
    for number, row in enumerate(rows, start=2):
        reference = (row.get("payout_reference") or "").strip()[:120]
        if not reference:
            continue
        try:
            line = {"number": number, "order": (row.get("order_reference") or "").strip(),
                "amount": parse_amount(row.get("amount")), "fee": parse_amount(row.get("fee"))}
            payout = grouped.setdefault(reference, {"paid_on": _date(row.get("payout_date")),
                "lines": []})
        except ValueError as error:
            raise PayoutImportError(f"Baris {number}: {error}") from error
        payout["lines"].append(line)
    return grouped


def _match(store, provider, line, claimed):
    """The succeeded payment a settlement row refers to, by the gateway or marketplace's own
    order or invoice number."""
    reference = line["order"]
    if not reference:
        raise PayoutImportError(f"Baris {line['number']}: order_reference kosong.")
    payment = PaymentTransaction.objects.filter(order__store=store, provider=provider,
        kind="payment", status="succeeded").filter(Q(external_id=reference) |
        Q(order__payment_reference=reference)).select_related("order").first()
    if not payment:
        raise PayoutImportError(f"Baris {line['number']}: pesanan {reference} tidak ditemukan "
            "atau belum dibayar.")
    if payment.id in claimed or PayoutLine.objects.filter(payment=payment).exists():
        raise PayoutImportError(f"Baris {line['number']}: pesanan {reference} sudah dicairkan.")
    if line["amount"] != payment.amount:
        raise PayoutImportError(f"Baris {line['number']}: nominal {reference} "
            f"Rp{line['amount']:,.0f} berbeda dari pembayaran Rp{payment.amount:,.0f}. Selisih "
            "potongan dicatat di kolom fee.")
    if not 0 <= line["fee"] <= line["amount"]:
        raise PayoutImportError(f"Baris {line['number']}: fee harus antara 0 dan nominal.")
    return payment


def import_payout(store, provider, reference, data, *, actor=None):
    """Record one payout and post it to the ledger. All or nothing: any row that does not match
    a paid order rejects the whole payout. Returns (payout, created)."""
    existing = Payout.objects.filter(store=store, provider=provider, reference=reference).first()
    if existing:
        return existing, False
    claimed, matched = set(), []
    for line in data["lines"]:
        payment = _match(store, provider, line, claimed)
        claimed.add(payment.id)
        matched.append((payment, line))
    gross = sum((line["amount"] for _, line in matched), Decimal(0))
    fees = sum((line["fee"] for _, line in matched), Decimal(0))
    try:
        with transaction.atomic():
            payout = Payout.objects.create(store=store, provider=provider, reference=reference,
                paid_on=data["paid_on"], gross=gross, fees=fees, net=gross - fees,
                created_by=actor)
            PayoutLine.objects.bulk_create(PayoutLine(payout=payout, payment=payment,
                amount=line["amount"], fee=line["fee"]) for payment, line in matched)
            post_payout(payout)
    except IntegrityError as error:  # the same payout or order settled concurrently
        raise PayoutImportError(f"Pencairan {reference} bentrok dengan impor lain; "
            "coba lagi.") from error
    return payout, True


def import_payouts(store, provider, grouped, *, actor=None):
    """Import each payout on its own, so one bad payout does not block the rest."""
    summary = {"created": 0, "duplicates": 0, "net": Decimal(0), "errors": []}
    for reference, data in grouped.items():
        try:
            payout, created = import_payout(store, provider, reference, data, actor=actor)
        except PayoutImportError as error:
            summary["errors"].append(f"{reference}: {error}")
            continue
        if created:
            summary["created"] += 1
            summary["net"] += payout.net
        else:
            summary["duplicates"] += 1
    return summary


def unsettled_payments(store):
    """Gateway and marketplace payments not yet covered by a payout."""
    return PaymentTransaction.objects.filter(order__store=store, kind="payment",
        status="succeeded", payout_line__isnull=True).exclude(
        provider__in=["bank_transfer", "demo"])


def outstanding_by_provider(store):
    return list(unsettled_payments(store).values("provider").annotate(count=Count("id"),
        total=Sum("amount"), oldest=Min("updated_at")).order_by("provider"))
