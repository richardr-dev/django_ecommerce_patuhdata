"""Recurring invoices: a template that issues an invoice on credit on each due date.

``run_due`` is idempotent: each (template, date) is recorded once, so running the worker twice,
or after a missed week, issues every invoice exactly once and in order."""
import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import RecurringInvoice, RecurringInvoiceItem, RecurringRun
from .sales import create_sales_invoice
from .services import InvoiceError

logger = logging.getLogger(__name__)

MAX_CATCH_UP = 12  # invoices one template may issue in a single run


class RecurringError(ValueError):
    pass


def advance(day, interval):
    if interval == "week":
        return day + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[interval]
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@transaction.atomic
def save_recurring(store, *, name, lines, buyer_name, interval, next_run, customer=None,
        company_name="", buyer_npwp="", phone="", address="", notes="", discount=0, shipping=0,
        due_days=14, end_date=None, recurring=None, actor=None):
    if not lines:
        raise RecurringError("Tambahkan minimal satu produk.")
    if interval not in dict(RecurringInvoice.INTERVALS):
        raise RecurringError("Pilih interval pengulangan.")
    if not (name.strip() and buyer_name.strip()):
        raise RecurringError("Nama jadwal dan nama pelanggan wajib diisi.")
    if end_date and end_date < next_run:
        raise RecurringError("Tanggal berakhir sebelum tanggal terbit pertama.")
    fields = {"customer": customer, "name": name.strip()[:160], "buyer_name": buyer_name.strip()[:160],
        "company_name": company_name[:160], "buyer_npwp": buyer_npwp[:16], "phone": phone[:30],
        "address": address, "notes": notes[:500], "discount": Decimal(discount or 0),
        "shipping": Decimal(shipping or 0), "interval": interval, "next_run": next_run,
        "end_date": end_date, "due_days": due_days}
    if recurring is None:
        recurring = RecurringInvoice.objects.create(store=store, created_by=actor, **fields)
    else:
        for key, value in fields.items():
            setattr(recurring, key, value)
        recurring.last_error = ""
        recurring.save()
        recurring.items.all().delete()
    RecurringInvoiceItem.objects.bulk_create(RecurringInvoiceItem(recurring=recurring,
        product=line["product"], quantity=line["qty"], unit_price=line["unit_price"])
        for line in lines)
    return recurring


def run_one(recurring, today=None):
    """Issue every invoice that has come due for one template. Returns the invoices."""
    today = today or timezone.localdate()
    issued = []
    for _ in range(MAX_CATCH_UP):
        with transaction.atomic():
            row = RecurringInvoice.objects.select_for_update().get(pk=recurring.pk)
            if (not row.is_active or row.next_run > today
                    or (row.end_date and row.next_run > row.end_date)):
                break
            run_date = row.next_run
            lines = [{"product": item.product, "qty": item.quantity, "unit_price": item.unit_price}
                for item in row.items.select_related("product")]
            try:
                with transaction.atomic():
                    RecurringRun.objects.create(recurring=row, run_date=run_date)
                    invoice = create_sales_invoice(row.store, lines=lines,
                        buyer_name=row.buyer_name, issued_on=run_date, customer=row.customer,
                        company_name=row.company_name, buyer_npwp=row.buyer_npwp,
                        phone=row.phone or "-", address=row.address or "-",
                        notes=row.notes or row.name, discount=row.discount, shipping=row.shipping,
                        on_credit=True, due_date=run_date + timedelta(days=row.due_days))
                    RecurringRun.objects.filter(recurring=row, run_date=run_date).update(
                        invoice=invoice)
            except IntegrityError:
                invoice = None  # already issued for this date by another worker
            except (InvoiceError, ValueError) as error:
                row.last_error = str(error)[:300]
                row.save(update_fields=["last_error"])
                logger.exception("recurring invoice %s failed", row.pk)
                break
            row.next_run = advance(run_date, row.interval)
            row.last_error = ""
            if invoice is not None:
                row.runs += 1
                issued.append(invoice)
            if row.end_date and row.next_run > row.end_date:
                row.is_active = False
            row.save(update_fields=["next_run", "runs", "is_active", "last_error"])
    return issued


def run_due(today=None, store=None):
    today = today or timezone.localdate()
    templates = RecurringInvoice.objects.filter(is_active=True, next_run__lte=today)
    if store:
        templates = templates.filter(store=store)
    issued = []
    for template in templates:
        issued += run_one(template, today)
    return issued
