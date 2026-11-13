"""The page a buyer opens from the link they were sent. No login; the token is the key."""
from django.http import Http404
from django.shortcuts import render

from apps.accounting.models import BankAccount
from apps.tax.models import CreditNote, Invoice, InvoicePayment

from . import sharing


def public_invoice(request, token):
    if len(token) < 20:
        raise Http404
    invoice = Invoice.objects.filter(public_token=token).exclude(status=Invoice.STATUS_DRAFT) \
        .select_related("store", "order").first()
    if invoice is None:
        raise Http404
    # The seller opening their own link while logged in does not count as the buyer viewing it.
    if not request.user.is_authenticated:
        sharing.record_view(invoice)
    paid = sum(p.amount for p in InvoicePayment.objects.filter(invoice=invoice))
    credited = sum(n.applied for n in CreditNote.objects.filter(invoice=invoice).exclude(
        status=CreditNote.STATUS_VOID))
    due = max(invoice.total - paid - credited, 0) if invoice.status != Invoice.STATUS_VOID else 0
    banks = BankAccount.objects.filter(store=invoice.store, is_active=True).exclude(
        account_number="").order_by("created_at")
    response = render(request, "tax/public_invoice.html", {"invoice": invoice, "items": invoice.items.all(),
        "paid": paid, "credited": credited, "due": due, "banks": banks,
        "settled": due <= 0 and invoice.status != Invoice.STATUS_VOID})
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
