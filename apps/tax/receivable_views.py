from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import Customer
from apps.shops.permissions import roles

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
    return render(request, "merchant/receivables.html", _merchant_context(request, store, reminder_policy=__import__("apps.tax.reminders", fromlist=["policy_for"]).policy_for(store),
        book_balance=book, unitemised=book - totals["total"], rows=rows, totals=totals, buckets=BUCKETS, as_of=as_of, customer=customer, lines=lines,
        owed=owed))
