"""Keuangan > Biaya operasional: the seller records what running the store costs (rent,
salaries, ads, packaging...). Each expense posts to the ledger at once, so Laporan & laba rugi
shows real net profit, and PPN masukan reduces the PPN a PKP seller pays."""
import csv
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .ledger import post_expense
from .models import Expense

VIEW_ROLES = {"admin", "manager", "finance", "viewer"}
EDIT_ROLES = {"admin", "finance"}
# The file is served from the back office's own origin, so its type is fixed by extension.
RECEIPT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "pdf": "application/pdf"}
MAX_RECEIPT = 5 * 1024 * 1024


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["date", "category", "description", "vendor", "amount", "input_vat", "receipt"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        labels = {"date": "Tanggal bayar", "category": "Kategori", "description": "Keterangan",
            "vendor": "Dibayar ke", "amount": "Jumlah dibayar (Rp)",
            "input_vat": "PPN masukan (Rp)", "receipt": "Nota / bukti bayar"}
        help_texts = {"amount": "Total yang keluar dari rekening, termasuk PPN bila ada.",
            "input_vat": "Khusus PKP: PPN di Faktur Pajak dari penjual. Kosongkan bila tidak ada.",
            "receipt": "Opsional. Foto atau PDF, maksimal 5 MB. Hanya tim Anda yang bisa membuka.",
            "vendor": "Opsional, mis. nama pemilik ruko atau Meta Ads."}

    def __init__(self, *args, is_pkp=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["input_vat"].required = False
        self.is_pkp = is_pkp

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Tanggal tidak boleh di masa depan.")
        return value

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value is None or value <= 0:
            raise forms.ValidationError("Jumlah harus lebih dari 0.")
        return value

    def clean_receipt(self):
        upload = self.cleaned_data.get("receipt")
        if upload and hasattr(upload, "content_type"):
            if upload.size > MAX_RECEIPT:
                raise forms.ValidationError("File terlalu besar (maksimal 5 MB).")
            extension = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
            if extension not in RECEIPT_TYPES:
                raise forms.ValidationError("Unggah foto (JPG, PNG, WebP) atau PDF.")
        return upload

    def clean(self):
        data = super().clean()
        amount = data.get("amount")
        vat = (data.get("input_vat") or Decimal(0)) if self.is_pkp else Decimal(0)
        if vat < 0:
            self.add_error("input_vat", "PPN masukan tidak boleh negatif.")
        elif amount and vat >= amount:
            self.add_error("input_vat", "PPN masukan harus lebih kecil dari jumlah dibayar.")
        data["input_vat"] = vat
        return data


def _month(request):
    try:
        year, month = (int(part) for part in request.GET.get("month", "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return timezone.localdate().replace(day=1)


def _next_month(first):
    return first.replace(year=first.year + (first.month == 12), month=first.month % 12 + 1)


def _is_pkp(store):
    from apps.tax.services import tax_profile

    return tax_profile(store).is_pkp


@login_required(login_url="/merchant/login/")
def merchant_expenses(request):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    month = _month(request)
    expenses = Expense.objects.filter(store=store, date__gte=month, date__lt=_next_month(month))
    if request.GET.get("format") == "csv":
        return _csv(store, month, expenses)
    initial = {"date": timezone.localdate()}
    copy = request.GET.get("copy")
    if copy and copy.isdigit():
        source = Expense.objects.filter(store=store, id=copy).first()
        if source:  # a monthly bill such as rent: same details, today's date
            initial |= {field: getattr(source, field) for field in
                ("category", "description", "vendor", "amount", "input_vat")}
    is_pkp = _is_pkp(store)
    form = ExpenseForm(request.POST or None, request.FILES or None, initial=initial, is_pkp=is_pkp)
    if request.method == "POST":
        _require_roles(request, store, EDIT_ROLES)
        if form.is_valid():
            with transaction.atomic():
                expense = form.save(commit=False)
                expense.store, expense.created_by = store, request.user
                expense.save()
                post_expense(expense)
                AuditLog.objects.create(store=store, actor=request.user, action="expense.created",
                    object_type="Expense", object_id=str(expense.id),
                    details={"amount": str(expense.amount), "category": expense.category,
                        "label": expense.description})
            messages.success(request, f"Biaya Rp{expense.amount:,.0f} dicatat dan masuk ke laba "
                "rugi.".replace(",", "."))
            return redirect(f"{request.path}?month={expense.date:%Y-%m}")
    by_category = (expenses.values("category").annotate(total=Sum("amount"))
        .order_by("-total"))
    labels = dict(Expense._meta.get_field("category").choices)
    previous = (month.replace(day=1) - date.resolution).replace(day=1)
    return render(request, "merchant/expenses.html", _merchant_context(request, store, form=form,
        expenses=expenses.select_related("created_by"), month=month, previous=previous,
        following=_next_month(month) if _next_month(month) <= timezone.localdate() else None,
        total=expenses.aggregate(total=Sum("amount", default=0))["total"],
        by_category=[{"label": labels.get(row["category"], row["category"]),
            "total": row["total"]} for row in by_category],
        edit_roles=EDIT_ROLES, is_pkp=is_pkp))


def _csv(store, month, expenses):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="biaya-{store.slug}-{month:%Y-%m}.csv"')
    writer = csv.writer(response)
    writer.writerow(["Tanggal", "Kategori", "Keterangan", "Dibayar ke", "Jumlah", "PPN masukan",
        "Biaya tanpa PPN", "Ada nota"])
    for expense in expenses.order_by("date", "id"):
        writer.writerow([expense.date.isoformat(), expense.get_category_display(),
            expense.description, expense.vendor, f"{expense.amount:.2f}",
            f"{expense.input_vat:.2f}", f"{expense.amount - expense.input_vat:.2f}",
            "ya" if expense.receipt else "tidak"])
    return response


@login_required(login_url="/merchant/login/")
def merchant_expense_delete(request, expense_id):
    """Remove a wrongly entered expense together with its journal entry."""
    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, EDIT_ROLES)
    expense = get_object_or_404(Expense, id=expense_id, store=store)
    with transaction.atomic():
        AuditLog.objects.create(store=store, actor=request.user, action="expense.removed",
            object_type="Expense", object_id=str(expense.id),
            details={"amount": str(expense.amount), "date": expense.date.isoformat(),
                "category": expense.category, "label": expense.description})
        entry = getattr(expense, "journal_entry", None)
        if entry:
            entry.delete()
        receipt = expense.receipt.name
        expense.delete()
        if receipt:
            transaction.on_commit(lambda: Expense._meta.get_field("receipt").storage
                .delete(receipt))
    messages.success(request, "Biaya dihapus dari pembukuan.")
    return redirect(f"/merchant/expenses/?month={expense.date:%Y-%m}")


@login_required(login_url="/merchant/login/")
def merchant_expense_receipt(request, expense_id):
    store = _staff_store(request)
    _require_roles(request, store, VIEW_ROLES)
    expense = get_object_or_404(Expense, id=expense_id, store=store)
    if not expense.receipt:
        raise Http404
    name = expense.receipt.name.rsplit("/", 1)[-1]
    content_type = RECEIPT_TYPES.get(name.rsplit(".", 1)[-1].lower())
    response = FileResponse(expense.receipt.open("rb"), filename=name,
        as_attachment=content_type is None,
        content_type=content_type or "application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
