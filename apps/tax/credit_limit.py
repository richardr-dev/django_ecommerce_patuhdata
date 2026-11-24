"""Batas kredit: how much a customer may owe on credit invoices before a new one is refused.

``Customer.credit_limit`` empty means no limit; 0 means cash only. The check runs when a person
sells on credit from the invoice form (`create_sales_invoice` / `update_sales_invoice` with
``credit_check=True``); automatic flows (kasir, marketplace import, recurring invoices, delivery
invoicing) are never blocked, the same rule as stok minus. Someone holding ``sales.credit`` may sell
past the limit; the view logs it.

Tahan kredit (``ReminderPolicy.credit_hold_days``, set on Piutang pelanggan): a store-wide rule that a
customer with an invoice more than N days past its due date gets no new credit sale until it is paid,
whatever their limit. It runs in the same check, is overridden by the same permission and logged as
``invoice.credit_hold_override``."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from apps.shops.audit import log as audit_log

from .models import Invoice
from .services import InvoiceError

OPEN = [Invoice.STATUS_ISSUED, Invoice.STATUS_PARTIAL, Invoice.STATUS_OVERDUE]


class CreditLimitExceeded(InvoiceError):
    pass


class CreditHold(CreditLimitExceeded):
    pass


def hold_days(store):
    from .models import ReminderPolicy
    return ReminderPolicy.objects.filter(store=store).values_list("credit_hold_days", flat=True).first()


def overdue_hold(customer, *, exclude=None, today=None):
    """``(invoice, days late)`` for the customer's oldest invoice past the store's hold, else None."""
    if customer is None:
        return None
    days = hold_days(customer.store)
    if days is None:
        return None
    from django.utils import timezone
    today = today or timezone.localdate()
    invoices = (Invoice.objects.filter(order__customer=customer, status__in=OPEN, due_date__lt=today - timedelta(days=days))
        .prefetch_related("payments", "credit_notes").order_by("due_date"))
    if exclude is not None:
        invoices = invoices.exclude(pk=exclude.pk)
    for invoice in invoices:
        if invoice.amount_due > 0:
            return invoice, (today - invoice.due_date).days
    return None


def held_customers(store, today=None):
    """``[(customer, invoice, days late)]`` for every customer the store's hold stops today, latest first."""
    days = hold_days(store)
    if days is None:
        return []
    from django.utils import timezone
    today = today or timezone.localdate()
    seen, rows = set(), []
    late = (Invoice.objects.filter(store=store, status__in=OPEN, order__customer__isnull=False,
        due_date__lt=today - timedelta(days=days)).select_related("order__customer")
        .prefetch_related("payments", "credit_notes").order_by("due_date"))
    for invoice in late:
        customer = invoice.order.customer
        if customer.pk in seen or invoice.amount_due <= 0:
            continue
        seen.add(customer.pk)
        rows.append((customer, invoice, (today - invoice.due_date).days))
    return rows


def hold_text(hold):
    invoice, late = hold
    return f"invoice {invoice.number} terlambat {late} hari"


@dataclass
class CreditPosition:
    limit: Decimal | None
    owed: Decimal  # open balance on the customer's invoices

    @property
    def available(self):
        return None if self.limit is None else max(self.limit - self.owed, Decimal(0))

    @property
    def used_pct(self):
        if not self.limit:
            return 100 if self.limit == 0 and self.owed else 0
        return min(100, int(self.owed * 100 / self.limit))


def owed(customer, *, exclude=None):
    """What the customer still owes on open invoices (``exclude``: an invoice being edited)."""
    invoices = Invoice.objects.filter(order__customer=customer, status__in=OPEN).prefetch_related(
        "payments", "credit_notes")
    if exclude is not None:
        invoices = invoices.exclude(pk=exclude.pk)
    return sum((invoice.amount_due for invoice in invoices), Decimal(0))


def position(customer):
    return CreditPosition(limit=customer.credit_limit, owed=owed(customer))


def check(customer, amount, *, exclude=None):
    """Refuse a credit sale of ``amount`` that would take the customer past the limit, or to a customer
    on hold for an invoice paid too late."""
    hold = overdue_hold(customer, exclude=exclude)
    if hold:
        raise CreditHold(f"Kredit {customer.name} ditahan: {hold_text(hold)} (batas toko {hold_days(customer.store)} "
            "hari). Minta pelunasan dulu, jual tunai, atau minta manajer/keuangan menyetujui.")
    if customer is None or customer.credit_limit is None:
        return
    current = owed(customer, exclude=exclude)
    if current + amount <= customer.credit_limit:
        return
    from apps.accounting.templatetags.books import rp
    if customer.credit_limit == 0:
        raise CreditLimitExceeded(f"{customer.name} hanya boleh membeli tunai (batas kredit 0). "
            "Ubah menjadi lunas, atau minta manajer/keuangan menyetujui penjualan kredit.")
    raise CreditLimitExceeded(f"Melewati batas kredit {customer.name}: piutang berjalan {rp(current)} "
        f"+ invoice ini {rp(amount)} melebihi batas {rp(customer.credit_limit)}. Minta pembayaran dulu, "
        "jual tunai, atau minta manajer/keuangan menyetujui.")


def log_override(store, actor, invoice, customer):
    """Record that a credit sale went past the limit with someone's say-so."""
    hold = overdue_hold(customer, exclude=invoice)
    if hold:
        audit_log(store, actor, "invoice.credit_hold_override", invoice, customer=customer.name,
            late_invoice=hold[0].number, days_late=hold[1])
    current = owed(customer)
    if customer.credit_limit is not None and current > customer.credit_limit:
        audit_log(store, actor, "invoice.credit_limit_override", invoice, limit=customer.credit_limit,
            owed=current, customer=customer.name)
