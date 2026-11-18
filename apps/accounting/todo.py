"""What the books are waiting for: the finance rows of Beranda's "Perlu tindakan" list. Each row
is ``{"label", "count", "url", "action", "icon", "tone"}``; nothing is shown when the count is 0."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse

from .models import BankStatementLine, FixedAsset


def depreciation_due(store, today):
    """Depreciation up to the end of last month that is not booked yet."""
    from . import assets
    last_month_end = today.replace(day=1) - timedelta(days=1)
    due = Decimal(0)
    for asset in FixedAsset.objects.filter(store=store, status=FixedAsset.ACTIVE,
            acquired_on__lte=last_month_end):
        planned = sum((row["amount"] for row in assets.schedule(asset)
            if row["month"] <= last_month_end), Decimal(0))
        due += max(planned - assets.accumulated(asset), Decimal(0))
    return due


def todo_items(store, today, user=None):
    from apps.channels.models import UnmappedSku
    from apps.tax.models import Invoice, RecurringInvoice

    from .payables import due_soon
    items = []

    def add(count, label, url, action, icon, tone=""):
        if count:
            items.append({"count": count, "label": label, "url": url, "action": action,
                "icon": icon, "tone": tone})

    from django.utils import timezone

    from apps.pos.models import PosShift
    stale = sum(1 for shift in PosShift.objects.filter(register__store=store, status=PosShift.OPEN)
        if timezone.localdate(shift.opened_at) < today)
    add(stale, "shift kasir belum ditutup sejak kemarin", reverse("merchant-pos"), "Hitung & tutup →", "cash", "warn")
    add(PosShift.objects.filter(register__store=store, status=PosShift.CLOSED, variance_pending_approval=True).count(),
        "selisih kas shift kasir menunggu persetujuan", reverse("merchant-approvals"), "Periksa →", "cash", "warn")
    from apps.tax.models import InvoicePayment
    stale_clearing = InvoicePayment.objects.filter(invoice__store=store, method__in=InvoicePayment.CLEARING_METHODS,
        paid_on__lt=today - timedelta(days=3)).count()
    add(stale_clearing, "pembayaran EDC/QRIS belum dicocokkan ke rekening", reverse("merchant-bank-accounts"),
        "Cocokkan →", "cash", "warn")

    from apps.tax import tax_calendar
    due = tax_calendar.attention(store, today)
    late = [r for r in due if r["status"] == "overdue"]
    add(len(late), "kewajiban pajak lewat jatuh tempo", reverse("merchant-tax-calendar"), "Periksa →", "alert", "danger")
    add(len(due) - len(late), "kewajiban pajak jatuh tempo dalam 7 hari", reverse("merchant-tax-calendar"), "Lihat →", "clock", "warn")
    from . import budgets
    from .approvals import pending_count
    add(pending_count(store, user) if user else 0, "permintaan menunggu persetujuan Anda",
        reverse("merchant-approvals"), "Periksa →", "check-circle", "warn")
    budget_alerts = budgets.alerts(store, today)
    add(len(budget_alerts["over"]), "pos biaya melebihi anggaran bulan ini", reverse("merchant-budget"),
        "Periksa →", "alert", "danger")
    add(len(budget_alerts["warn"]), "pos biaya hampir habis (≥ 80% anggaran)", reverse("merchant-budget"),
        "Lihat →", "trend-down", "warn")
    overdue = Invoice.objects.filter(store=store, due_date__lt=today,
        status__in=[Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]).count()
    add(overdue, "invoice lewat jatuh tempo", reverse("merchant-receivables"), "Tagih →", "clock", "danger")
    claimed = Invoice.objects.filter(store=store, transfer_claimed_at__isnull=False).exclude(
        status__in=[Invoice.STATUS_PAID, Invoice.STATUS_VOID]).count()
    add(claimed, "konfirmasi transfer dari pembeli menunggu dicek", reverse("merchant-sales-invoices") + "?crm=&tab=unpaid",
        "Cek mutasi →", "cash", "warn")
    bills = due_soon(store, today)
    add(len(bills), "tagihan supplier jatuh tempo dalam 7 hari", reverse("merchant-payables"),
        "Bayar →", "truck")
    add(BankStatementLine.objects.filter(store=store, status="unmatched").count(),
        "mutasi bank belum dicocokkan", reverse("merchant-bank-accounts"), "Cocokkan →", "bank")
    add(RecurringInvoice.objects.filter(store=store, is_active=True, next_run__lte=today).count(),
        "invoice berulang siap diterbitkan", reverse("merchant-recurring"), "Terbitkan →", "file")
    add(UnmappedSku.objects.filter(store=store, resolved=False).count(),
        "SKU marketplace belum dipetakan", reverse("merchant-sku-map"), "Petakan →", "link")
    due = depreciation_due(store, today)
    if due:
        add(1, f"penyusutan aset belum dicatat (Rp{due:,.0f})".replace(",", "."),
            reverse("merchant-assets"), "Catat →", "layers")
    return items
