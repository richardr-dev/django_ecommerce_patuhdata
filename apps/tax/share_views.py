"""Sending documents and what the buyer does with them: the public quote page (view, accept,
decline), the buyer's "sudah transfer" on the public invoice, sending a quote by link, WhatsApp or
email, sending through the store's connected WhatsApp account, the proforma from a quote, and the
automatic-reminder policy. The invoice link, email and WhatsApp link live in channels/views.py."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounting.models import BankAccount
from apps.orders.views import _require_roles, _staff_store
from apps.shops.audit import log as audit_log
from apps.shops.permissions import roles

from . import quotes, reminders, sharing
from .models import Invoice, Quote, ReminderLog, ReminderPolicy

LOGIN = "/login/"
EDIT_ROLES = roles("sales.edit")
VIEW_ROLES = roles("sales.view")


def _public_quote(token):
    if len(token) < 20:
        raise Http404
    quote = Quote.objects.filter(public_token=token).select_related("store", "invoice").first()
    if quote is None:
        raise Http404
    return quote


def _no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def public_quote(request, token):
    """The buyer's view of a quote: lines, totals, validity, accept or decline."""
    quote = _public_quote(token)
    if not request.user.is_authenticated:
        sharing.record_quote_view(quote)
    banks = BankAccount.objects.filter(store=quote.store, is_active=True).exclude(account_number="")
    return _no_store(render(request, "tax/public_quote.html", {"quote": quote, "store": quote.store,
        "items": quote.items.all(), "expired": quotes.is_expired(quote), "banks": banks,
        "can_respond": quote.status in {Quote.STATUS_DRAFT, Quote.STATUS_SENT} and not quotes.is_expired(quote),
        "done": request.GET.get("ok", "")}))


@require_POST
def public_quote_respond(request, token):
    quote = _public_quote(token)
    action = request.POST.get("action")
    if action not in {"accept", "decline"} or quote.status not in {Quote.STATUS_DRAFT, Quote.STATUS_SENT} \
            or quotes.is_expired(quote):
        raise Http404
    quotes.set_status(quote, Quote.STATUS_ACCEPTED if action == "accept" else Quote.STATUS_DECLINED)
    Quote.objects.filter(pk=quote.pk).update(responded_at=timezone.now(),
        response_note=request.POST.get("note", "").strip()[:300])
    audit_log(quote.store, None, "quote.accepted" if action == "accept" else "quote.declined", quote,
        by="pembeli (tautan)", note=request.POST.get("note", "").strip()[:300])
    return redirect(f"{request.path.rsplit('respond/', 1)[0]}?ok={action}")


@require_POST
def public_invoice_confirm(request, token):
    """The buyer says they transferred: the seller sees it on Beranda and checks the bank."""
    if len(token) < 20:
        raise Http404
    invoice = Invoice.objects.filter(public_token=token).exclude(status=Invoice.STATUS_DRAFT).first()
    if invoice is None or invoice.status in {Invoice.STATUS_VOID, Invoice.STATUS_PAID}:
        raise Http404
    note = " · ".join(part for part in (request.POST.get("bank", "").strip()[:80],
        request.POST.get("date", "").strip()[:20], request.POST.get("amount", "").strip()[:30],
        request.POST.get("note", "").strip()[:120]) if part)[:300]
    Invoice.objects.filter(pk=invoice.pk).update(transfer_claimed_at=timezone.now(), transfer_claim_note=note)
    audit_log(invoice.store, None, "invoice.transfer_claimed", invoice, by="pembeli (tautan)", note=note)
    return redirect(f"{request.path.rsplit('konfirmasi/', 1)[0]}?ok=transfer")


# ── seller side ──────────────────────────────────────────────────────────────

def _quote(store, quote_id):
    return get_object_or_404(Quote.objects.select_related("store", "customer"), pk=quote_id, store=store)


@login_required(login_url=LOGIN)
def merchant_quote_proforma(request, quote_id):
    """Proforma invoice from a quote: the invoice's look, the quote's lines, how to pay, and a note
    that it is neither a tax invoice nor a receipt. Books nothing."""
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES | {"viewer"})
    quote = _quote(store, quote_id)
    banks = BankAccount.objects.filter(store=store, is_active=True).exclude(account_number="").order_by("created_at")
    return render(request, "tax/proforma.html", {"quote": quote, "store": store, "items": quote.items.all(),
        "banks": banks, "today": timezone.localdate(), "number": f"PRO-{quote.number}"})


@login_required(login_url=LOGIN)
@require_POST
def merchant_quote_email(request, quote_id):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    quote = _quote(store, quote_id)
    try:
        sharing.send_quote_email(quote, sharing.quote_public_url(request, quote), request.POST.get("to", ""),
            note=request.POST.get("note", ""))
    except (sharing.ShareError, OSError) as error:
        messages.error(request, str(error) or "Email gagal dikirim.")
    else:
        audit_log(store, request.user, "quote.sent", quote, channel="email", to=request.POST.get("to", "").strip())
        messages.success(request, f"Penawaran {quote.number} dikirim ke {request.POST.get('to', '').strip()}.")
    return redirect("merchant-quote-detail", quote.pk)


@login_required(login_url=LOGIN)
@require_POST
def merchant_quote_sent(request, quote_id):
    """The WhatsApp link was opened: remember the quote went out."""
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    quote = _quote(store, quote_id)
    sharing.mark_quote_sent(quote)
    return HttpResponse(status=204)


def _whatsapp_account(store):
    from apps.integrations.models import StoreWhatsAppSettings
    account = StoreWhatsAppSettings.objects.filter(store=store).first()
    return account if account is not None and account.ready else None


def _send_whatsapp(request, store, *, kind, phone, text, order=None):
    from apps.integrations.http import ProviderError
    from apps.integrations.whatsapp import msisdn, send
    account = _whatsapp_account(store)
    to = msisdn(phone)
    if account is None:
        raise sharing.ShareError("WhatsApp belum terhubung. Hubungkan di Pengaturan › WhatsApp, atau pakai tautan WhatsApp.")
    if not to:
        raise sharing.ShareError("Nomor WhatsApp pembeli tidak ada atau tidak valid.")
    try:
        message = send(account, kind=kind, to=to, text=text, order=order)
    except ProviderError as error:
        raise sharing.ShareError(f"WhatsApp gagal: {error}") from None
    if message.status != "sent":
        raise sharing.ShareError(f"WhatsApp ditolak: {message.error}")
    return to


@login_required(login_url=LOGIN)
@require_POST
def merchant_quote_whatsapp(request, quote_id):
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    quote = _quote(store, quote_id)
    try:
        to = _send_whatsapp(request, store, kind="quote", phone=quote.phone,
            text=sharing.quote_message_text(quote, sharing.quote_public_url(request, quote)))
    except sharing.ShareError as error:
        messages.error(request, str(error))
    else:
        sharing.mark_quote_sent(quote)
        audit_log(store, request.user, "quote.sent", quote, channel="whatsapp", to=to)
        messages.success(request, f"Penawaran {quote.number} dikirim lewat WhatsApp ke {to}.")
    return redirect("merchant-quote-detail", quote.pk)


@login_required(login_url=LOGIN)
@require_POST
def merchant_invoice_whatsapp(request, invoice_id):
    """Send the invoice (or the reminder when it is late) from the store's own WhatsApp account."""
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    invoice = get_object_or_404(Invoice.objects.select_related("order"), pk=invoice_id, store=store)
    if invoice.status in {Invoice.STATUS_DRAFT, Invoice.STATUS_VOID}:
        raise Http404
    try:
        to = _send_whatsapp(request, store, kind="invoice", phone=invoice.order.phone, order=invoice.order,
            text=sharing.message_text(invoice, sharing.public_url(request, invoice), invoice.amount_due))
    except sharing.ShareError as error:
        messages.error(request, str(error))
    else:
        sharing.mark_sent(invoice)
        audit_log(store, request.user, "invoice.sent", invoice, channel="whatsapp", to=to)
        messages.success(request, f"Invoice {invoice.number} dikirim lewat WhatsApp ke {to}.")
    return redirect("merchant-invoice-detail", invoice_id=invoice.pk)


@login_required(login_url=LOGIN)
@require_POST
def merchant_reminder_policy(request):
    """Save the automatic reminder policy (Piutang pelanggan › Pengingat otomatis)."""
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    policy = reminders.policy_for(store)
    before = request.POST.get("days_before", "").strip()
    policy.enabled = bool(request.POST.get("enabled"))
    policy.days_before = int(before) if before.isdigit() and int(before) <= 60 else 0
    policy.days_after = ",".join(str(d) for d in reminders.offsets(ReminderPolicy(
        days_after=request.POST.get("days_after", ""))))
    policy.use_email = bool(request.POST.get("use_email"))
    policy.use_whatsapp = bool(request.POST.get("use_whatsapp"))
    policy.save()
    audit_log(store, request.user, "reminders.policy_updated", policy, enabled=policy.enabled,
        before=policy.days_before, after=policy.days_after)
    messages.success(request, "Pengingat otomatis " + ("diaktifkan." if policy.enabled else "dimatikan."))
    return redirect("merchant-receivables")


@login_required(login_url=LOGIN)
@require_POST
def merchant_credit_hold_policy(request):
    """Tahan kredit (Piutang pelanggan): no new credit sale to a customer with an invoice N days late."""
    store = _staff_store(request)
    _require_roles(request, store, roles("sales.credit"))
    policy = reminders.policy_for(store)
    raw = request.POST.get("credit_hold_days", "").strip()
    policy.credit_hold_days = int(raw) if request.POST.get("hold") and raw.isdigit() and int(raw) <= 365 else None
    policy.save()
    audit_log(store, request.user, "credit_hold.policy_updated", policy, days=policy.credit_hold_days)
    messages.success(request, "Kredit ditahan untuk pelanggan dengan invoice terlambat lebih dari "
        f"{policy.credit_hold_days} hari." if policy.credit_hold_days is not None else "Tahan kredit dimatikan.")
    return redirect("merchant-receivables")


def reminder_rows(invoice):
    return ReminderLog.objects.filter(invoice=invoice).order_by("-created_at")[:10]
