from django import forms

from .models import SalesChannel


class SalesChannelForm(forms.ModelForm):
    class Meta:
        model = SalesChannel
        fields = ["platform", "name"]
        labels = {"platform": "Marketplace", "name": "Nama toko di marketplace"}


class OrdersImportForm(forms.Form):
    file = forms.FileField(
        label="File pesanan",
        help_text="XLSX dari Shopee Seller Center, atau CSV template Lakuin. Maks 10 MB.",
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File terlalu besar (maksimal 10 MB).")
        name = upload.name.lower()
        if not (name.endswith(".csv") or name.endswith(".xlsx")):
            raise forms.ValidationError("Format tidak didukung. Gunakan .xlsx atau .csv.")
        return upload


# Keep old name as alias so existing imports don't break.
OrdersCsvForm = OrdersImportForm
