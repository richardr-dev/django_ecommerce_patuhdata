import csv

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.billing.plans import can_reconcile_payouts
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .models import Payout
from .payouts import (
    PAYOUT_COLUMNS,
    PayoutImportError,
    import_payouts,
    outstanding_by_provider,
    parse_payouts_csv,
    payout_providers,
    unsettled_payments,
)

VIEW_ROLES = {"admin", "manager", "finance", "viewer"}
EDIT_ROLES = {"admin", "finance"}


class PayoutUploadForm(forms.Form):
    provider = forms.ChoiceField(label="Dicairkan oleh", choices=payout_providers)
    file = forms.FileField(label="File CSV pencairan",
        help_text="Template Lakuin: satu baris per pesanan, maksimal 2 MB.")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("File terlalu besar (maksimal 2 MB).")
        return upload


@login_required(login_url="/merchant/login/")
def merchant_payouts(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    form, summary = PayoutUploadForm(request.POST or None, request.FILES or None), None
    can_reconcile = can_reconcile_payouts(store)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if not can_reconcile:
            form.add_error(None, "Rekonsiliasi pencairan tersedia mulai paket Pro.")
        elif form.is_valid():
            provider = form.cleaned_data["provider"]
            try:
                grouped = parse_payouts_csv(form.cleaned_data["file"])
            except PayoutImportError as error:
                form.add_error("file", str(error))
            else:
                summary = import_payouts(store, provider, grouped, actor=request.user)
                AuditLog.objects.create(store=store, actor=request.user,
                    action="payouts.imported", object_type="Payout", object_id=provider,
                    details={key: summary[key] for key in ("created", "duplicates")}
                        | {"errors": len(summary["errors"])})
    labels = dict(payout_providers())
    outstanding = outstanding_by_provider(store)
    today = timezone.localdate()
    for row in outstanding:
        row["label"] = labels.get(row["provider"], row["provider"])
        row["age_days"] = (today - timezone.localdate(row["oldest"])).days
    oldest = unsettled_payments(store).select_related("order").order_by("updated_at")[:50]
    payouts = Payout.objects.filter(store=store)[:50]
    for payout in payouts:
        payout.label = labels.get(payout.provider, payout.provider)
        payout.fee_rate = payout.fees * 100 / payout.gross if payout.gross else 0
    return render(request, "merchant/payouts.html", _merchant_context(request, store, form=form,
        summary=summary, outstanding=outstanding, oldest=oldest, payouts=payouts, today=today,
        can_reconcile=can_reconcile))


@login_required(login_url="/merchant/login/")
def merchant_payout_detail(request, payout_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    payout = get_object_or_404(Payout, id=payout_id, store=store)
    payout.label = dict(payout_providers()).get(payout.provider, payout.provider)
    lines = payout.lines.select_related("payment__order").order_by("id")
    return render(request, "merchant/payout_detail.html", _merchant_context(request, store,
        payout=payout, lines=lines))


@login_required(login_url="/merchant/login/")
def payout_template(request):
    _require_roles(request, _staff_store(request), VIEW_ROLES)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="template-pencairan.csv"'
    writer = csv.writer(response)
    writer.writerow(PAYOUT_COLUMNS)
    writer.writerow(["PO-20261102", "2026-11-02", "NK1A2B3C4D", "121000", "2420"])
    return response
