from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import Customer
from apps.shops.permissions import can, roles

from . import credit_limit, reminders
from .receivables import BUCKETS, aging, statement

ROLES = roles("books.view")


@login_required(login_url="/login/")
def merchant_receivables(request):
    """Umur piutang per pelanggan, dan surat tagihan satu pelanggan."""
    store = _staff_store(request)
    _require_roles(request, store, ROLES)
    as_of = parse_date(request.GET.get("as_of", "")) or timezone.localdate()
    customer = None
    lines, owed = [], None
    if request.GET.get("customer"):
        try:
            customer = get_object_or_404(Customer, pk=request.GET["customer"], store=store)
        except (ValueError, ValidationError):
            raise Http404 from None
        lines, owed = statement(store, customer.pk, as_of)
    rows, totals = aging(store, as_of)
    from apps.accounting.ledger import RECEIVABLE, account_balances
    book = {a.code: a.balance for a in account_balances(store, end=as_of)}.get(RECEIVABLE, 0)
    return render(request, "merchant/receivables.html", _merchant_context(request, store, reminder_policy=reminders.policy_for(store),
        book_balance=book, unitemised=book - totals["total"], rows=rows, totals=totals, buckets=BUCKETS, as_of=as_of, customer=customer, lines=lines,
        owed=owed, held=credit_limit.held_customers(store),
        may_set_credit=can(request.user, store, "sales.credit")))


@login_required(login_url="/login/")
def merchant_customer_statement(request, customer_id):
    """Laporan rekening pelanggan for a period: a branded page to print or save as PDF and send."""
    from apps.accounting.models import BankAccount

    from .customer_statement import build
    store = _staff_store(request)
    _require_roles(request, store, ROLES | roles("sales.view"))
    customer = get_object_or_404(Customer, pk=customer_id, store=store)
    today = timezone.localdate()
    end = parse_date(request.GET.get("sampai", "")) or today
    start = parse_date(request.GET.get("dari", "")) or end.replace(day=1)
    if start > end:
        start, end = end, start
    banks = BankAccount.objects.filter(store=store, is_active=True).exclude(account_number="").order_by("created_at")
    return render(request, "tax/customer_statement.html", {"store": store, "customer": customer,
        "s": build(store, customer, start, end), "banks": banks, "today": today})
