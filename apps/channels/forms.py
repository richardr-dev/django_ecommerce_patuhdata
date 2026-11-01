from django import forms

from .models import SalesChannel


class SalesChannelForm(forms.ModelForm):
    class Meta:
        model = SalesChannel
        fields = ["platform", "name"]
        labels = {"platform": "Marketplace", "name": "Nama toko di marketplace"}


class OrdersCsvForm(forms.Form):
    file = forms.FileField(label="File CSV pesanan",
        help_text="Gunakan template Lakuin: satu baris per produk, maksimal 2 MB.")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("File terlalu besar (maksimal 2 MB).")
        return upload
