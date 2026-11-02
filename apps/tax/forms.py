from django import forms

from .models import TaxProfile
from .services import normalize_npwp


class TaxProfileForm(forms.ModelForm):
    # Wider than the stored 16 digits so a number typed with dots and dashes still validates.
    npwp = forms.CharField(label="NPWP", required=False, max_length=24,
        help_text=TaxProfile._meta.get_field("npwp").help_text)

    class Meta:
        model = TaxProfile
        fields = ["legal_name", "npwp", "address", "taxpayer_type", "pph_regime", "is_pkp",
            "invoice_prefix"]
        labels = {"legal_name": "Nama wajib pajak", "npwp": "NPWP", "address": "Alamat NPWP",
            "taxpayer_type": "Jenis wajib pajak", "pph_regime": "Skema PPh",
            "invoice_prefix": "Awalan nomor invoice"}
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def clean_npwp(self):
        try:
            return normalize_npwp(self.cleaned_data["npwp"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error

    def clean_invoice_prefix(self):
        return self.cleaned_data["invoice_prefix"].upper()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_pkp") and not cleaned.get("npwp"):
            self.add_error("npwp", "PKP wajib mengisi NPWP.")
        return cleaned


class BuyerNpwpForm(forms.Form):
    buyer_npwp = forms.CharField(label="NPWP/NIK pembeli", required=False, max_length=24)

    def clean_buyer_npwp(self):
        try:
            return normalize_npwp(self.cleaned_data["buyer_npwp"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error
