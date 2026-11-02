import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.access import get_customer_order_or_404
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .forms import BuyerNpwpForm, TaxProfileForm
from .models import Invoice
from .services import money, month_bounds, monthly_tax, tax_profile

VIEW_ROLES = {"admin", "manager", "finance", "viewer"}
EDIT_ROLES = {"admin", "finance"}


def _month(request):
    """(year, month) from ?month=YYYY-MM, defaulting to the current month."""
    today = timezone.localdate()
    try:
        year, month = (int(part) for part in request.GET.get("month", "").split("-"))
        if 2000 <= year <= today.year and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    return today.year, today.month


def _warnings(store, profile):
    if profile.is_pkp and not store.tax_rate:
        return [("Anda PKP tetapi tarif PPN toko 0%. Atur tarif PPN di Brand & konten "
            "(11% untuk barang umum: 12% × DPP nilai lain 11/12).")]
    if not profile.is_pkp and store.tax_rate:
        return [(f"Toko memungut PPN {store.tax_rate}% padahal belum PKP. Non-PKP tidak boleh "
            "memungut PPN: ubah tarif menjadi 0% atau tandai PKP setelah dikukuhkan.")]
    return []


@login_required(login_url="/merchant/login/")
def merchant_tax(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    profile = tax_profile(store)
    form = TaxProfileForm(request.POST or None, instance=profile)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if form.is_valid():
            form.instance.store = store
            form.save()
            AuditLog.objects.create(store=store, actor=request.user, action="tax_profile.updated",
                object_type="TaxProfile", object_id=str(form.instance.id),
                details={"is_pkp": form.instance.is_pkp, "regime": form.instance.pph_regime})
            messages.success(request, "Profil pajak disimpan. Berlaku untuk invoice berikutnya.")
            return redirect("merchant-tax")
    year, month = _month(request)
    report = monthly_tax(store, year, month, profile)
    invoices = Invoice.objects.filter(store=store, issued_on__range=(report.start, report.end))
    return render(request, "merchant/tax.html", _merchant_context(request, store, form=form,
        profile=profile, report=report, invoices=invoices[:100], month=f"{year}-{month:02d}",
        warnings=_warnings(store, profile)))


@login_required(login_url="/merchant/login/")
def merchant_invoice_register(request):
    """Invoices of one month as CSV, laid out for preparing Faktur Pajak in Coretax."""
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    year, month = _month(request)
    invoices = Invoice.objects.filter(store=store, issued_on__range=month_bounds(year, month)
        ).annotate(refunded=Sum("order__refunds__amount", default=0,
        filter=Q(order__refunds__status="succeeded"))).order_by("issued_on", "number")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="invoice-{store.slug}-{year}-{month:02d}.csv"')
    writer = csv.writer(response)
    writer.writerow(["Nomor invoice", "Tanggal", "No. pesanan", "Nama pembeli",
        "NPWP/NIK pembeli", "Alamat pembeli", "DPP", "DPP nilai lain (11/12)", "Tarif PPN (%)",
        "PPN", "Ongkir", "Total", "Refund"])
    for invoice in invoices.iterator():
        # PKP selling ordinary goods at the 12% rate use 11/12 of the price as the tax base.
        other_base = money(invoice.tax_base * 11 / 12) if invoice.seller_is_pkp else ""
        writer.writerow([invoice.number, invoice.issued_on.isoformat(), invoice.order_id,
            invoice.buyer_name, invoice.buyer_npwp, invoice.buyer_address, invoice.tax_base,
            other_base, invoice.tax_rate, invoice.tax, invoice.shipping, invoice.total,
            invoice.refunded])
    return response


@login_required(login_url="/merchant/login/")
def merchant_invoice(request, invoice_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    invoice = get_object_or_404(Invoice.objects.select_related("order"), id=invoice_id,
        store=store)
    form = BuyerNpwpForm(request.POST or None, initial={"buyer_npwp": invoice.buyer_npwp})
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if form.is_valid():
            invoice.buyer_npwp = form.cleaned_data["buyer_npwp"]
            invoice.save(update_fields=["buyer_npwp"])
            AuditLog.objects.create(store=store, actor=request.user,
                action="invoice.buyer_npwp_updated", object_type="Invoice",
                object_id=str(invoice.id), details={"number": invoice.number})
            messages.success(request, "NPWP pembeli disimpan.")
            return redirect("merchant-invoice", invoice.id)
    role_context = _merchant_context(request, store)
    return render(request, "tax/invoice.html", {"invoice": invoice, "form": form,
        "can_edit": role_context["merchant_role"] in EDIT_ROLES})


def order_invoice(request, order_id):
    """The buyer's copy, opened from the order page."""
    if not request.store:
        raise Http404
    order = get_customer_order_or_404(request, order_id)
    invoice = get_object_or_404(Invoice, order=order)
    return render(request, "tax/invoice.html", {"invoice": invoice})
