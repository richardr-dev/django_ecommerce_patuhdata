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
        help_text="XLSX dari Shopee Seller Center, atau CSV template Patuhdata. Maks 10 MB.",
    )

    @staticmethod
    def check_upload(upload):
        """The reason an uploaded file is refused, else an empty string."""
        if upload.size > 10 * 1024 * 1024:
            return "File terlalu besar (maksimal 10 MB)."
        name = upload.name.lower()
        if not name.endswith((".csv", ".xlsx")):
            return "Format tidak didukung. Gunakan .xlsx atau .csv."
        return ""

    def clean_file(self):
        upload = self.cleaned_data["file"]
        problem = self.check_upload(upload)
        if problem:
            raise forms.ValidationError(problem)
        return upload


# Keep old name as alias so existing imports don't break.
OrdersCsvForm = OrdersImportForm
