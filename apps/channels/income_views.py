from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.accounting.models import BankAccount
from apps.orders.views import _merchant_context, _require_roles, _staff_store
from apps.shops.models import AuditLog

from .income import import_income
from .models import ChannelOrderIncome, SalesChannel
from .parsers.income import parse_income
from .services import ChannelImportError

INCOME_ROLES = {"admin", "finance"}


class IncomeUploadForm(forms.Form):
    channel = forms.ModelChoiceField(SalesChannel.objects.none(), label="Toko marketplace")
    file = forms.FileField(label="Laporan penghasilan (XLSX atau CSV)",
        help_text="Unduh dari menu Penghasilan / Settlement di Seller Center. Maks 10 MB.")
    release_date = forms.DateField(required=False, label="Tanggal pencairan (jika tidak ada di file)",
        widget=forms.DateInput(attrs={"type": "date"}))
    bank_account = forms.ModelChoiceField(BankAccount.objects.none(), required=False,
        empty_label="Kas utama", label="Dana masuk ke rekening")

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].queryset = SalesChannel.objects.filter(store=store, is_active=True)
        from apps.accounting.bank import bank_accounts
        self.fields["bank_account"].queryset = BankAccount.objects.filter(
            pk__in=[a.pk for a in bank_accounts(store)])

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File terlalu besar (maksimal 10 MB).")
        if not upload.name.lower().endswith((".csv", ".xlsx")):
            raise forms.ValidationError("Format tidak didukung. Gunakan .xlsx atau .csv.")
        return upload


@login_required(login_url="/merchant/login/")
def merchant_income_import(request):
    """Impor laporan penghasilan marketplace: biaya per pesanan dan pencairan dana."""
    store = _staff_store(request)
    _require_roles(request, store, INCOME_ROLES)
    form = IncomeUploadForm(request.POST or None, request.FILES or None, store=store)
    summary = None
    if request.method == "POST" and form.is_valid():
        channel, upload = form.cleaned_data["channel"], form.cleaned_data["file"]
        try:
            rows = parse_income(upload, upload.name)
            summary = import_income(channel, rows, actor=request.user,
                default_release=form.cleaned_data["release_date"],
                bank_account=form.cleaned_data["bank_account"])
            AuditLog.objects.create(store=store, actor=request.user, action="channel.income_imported",
                object_type="SalesChannel", object_id=str(channel.id),
                details={"orders": summary["orders"], "payouts": summary["payouts"],
                    "errors": len(summary["errors"])})
            if summary["orders"]:
                messages.success(request, f"{summary['orders']} pesanan dicocokkan dengan "
                    f"pencairan dana ({summary['payouts']} pencairan baru).")
        except (ChannelImportError, UnicodeDecodeError) as error:
            form.add_error("file", f"File tidak dapat dibaca: {error}")
    recent = ChannelOrderIncome.objects.filter(channel_order__channel__store=store).select_related(
        "channel_order__channel", "channel_order__order").order_by("-created_at")[:20]
    return render(request, "merchant/income_import.html", _merchant_context(request, store,
        form=form, summary=summary, recent=recent))
