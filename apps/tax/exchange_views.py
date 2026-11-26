"""Tukar barang from the invoice page (``tax/exchanges.py``): the dialog posts here; the service does the rest."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from apps.accounting import idempotency
from apps.orders.views import _require_roles, _staff_store
from apps.shops.permissions import roles

from .exchanges import ExchangeError, exchange_line
from .models import Invoice

SCOPE = "invoice_exchange"


@login_required(login_url="/login/")
@require_POST
def merchant_invoice_exchange(request, invoice_id):
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.edit"))
    invoice = get_object_or_404(Invoice, pk=invoice_id, store=store)
    idem = request.POST.get("idem", "")
    earlier = idempotency.claim(store, SCOPE, idem)
    if earlier is not None:
        messages.info(request, "Penukaran ini sudah disimpan.")
        return redirect("merchant-invoice-detail", invoice.pk)
    price = "".join(ch for ch in request.POST.get("unit_price", "") if ch.isdigit())
    try:
        swap = exchange_line(invoice, item=request.POST.get("item"), quantity=request.POST.get("quantity") or 1,
            replacement_code=request.POST.get("replacement", ""), condition=request.POST.get("condition", "good"),
            unit_price=price or None, issued_on=parse_date(request.POST.get("issued_on", "")),
            reason=request.POST.get("reason", "").strip(), actor=request.user)
    except ExchangeError as error:
        idempotency.release(store, SCOPE, idem)
        messages.error(request, str(error))
        return redirect("merchant-invoice-detail", invoice.pk)
    idempotency.finish(store, SCOPE, idem, swap.pk)
    new = swap.replacement_invoice
    new.refresh_from_db()
    due = f" Sisa tagihan pengganti Rp{new.amount_due:,.0f}.".replace(",", ".") if new.amount_due else \
        " Invoice pengganti lunas dari nota kredit."
    messages.success(request, f"Barang ditukar: nota kredit {swap.credit_note.number} dan invoice pengganti "
        f"{new.number} dibuat.{due}")
    return redirect("merchant-invoice-detail", invoice.pk)
