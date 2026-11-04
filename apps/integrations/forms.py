import re

from django import forms

from .models import StorePaymentSettings


class PaymentSettingsForm(forms.ModelForm):
    # Secrets are write-only: left blank, the saved value is kept.
    secret_key = forms.CharField(required=False, strip=True, label="Secret key",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="DOKU: Secret Key. Xendit: Secret API Key. Kosongkan untuk memakai yang tersimpan.")
    webhook_token = forms.CharField(required=False, strip=True, label="Token verifikasi webhook",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Khusus Xendit: Callback Verification Token di dashboard Xendit.")

    class Meta:
        model = StorePaymentSettings
        fields = ["bank_transfer_enabled", "bank_name", "bank_account_number", "bank_account_holder",
            "gateway", "doku_client_id", "use_platform_gateway"]
        widgets = {"use_platform_gateway": forms.CheckboxInput(attrs={"class": "switch",
            "role": "switch"})}
        labels = {"bank_transfer_enabled": "Terima transfer bank", "bank_name": "Nama bank",
            "bank_account_number": "Nomor rekening", "bank_account_holder": "Nama pemilik rekening",
            "gateway": "Payment gateway", "doku_client_id": "DOKU Client ID",
            "use_platform_gateway": "Terima pembayaran lewat DOKU Lakuin (PT Patuhdata Solusi Nusantara)"}
        help_texts = {"bank_transfer_enabled": "Pembeli transfer langsung ke rekening Anda, lalu Anda "
            "konfirmasi dana masuk di halaman Pesanan.",
            "gateway": "Hubungkan akun DOKU atau Xendit milik usaha Anda sendiri.",
            "use_platform_gateway": "Aktifkan untuk menerima QRIS, virtual account, dan e-wallet "
                "tanpa mendaftar DOKU sendiri; matikan kapan saja. Dana diterima PT Patuhdata Solusi "
                "Nusantara lalu ditransfer ke rekening di atas setelah pesanan diterima pembeli."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved_gateway = self.instance.gateway

    def clean_bank_account_number(self):
        number = re.sub(r"[\s.-]", "", self.cleaned_data["bank_account_number"])
        if number and not number.isdigit():
            raise forms.ValidationError("Nomor rekening hanya berisi angka.")
        return number

    def clean(self):
        data = super().clean()
        if data.get("bank_transfer_enabled") and not all(
                data.get(field) for field in ["bank_name", "bank_account_number", "bank_account_holder"]):
            self.add_error("bank_transfer_enabled", "Lengkapi nama bank, nomor, dan pemilik rekening.")
        if data.get("use_platform_gateway") and not all(
                data.get(field) for field in ["bank_name", "bank_account_number", "bank_account_holder"]):
            self.add_error("use_platform_gateway", "Isi rekening bank tujuan pencairan dana terlebih dahulu.")
        gateway = data.get("gateway")
        # A key saved for another gateway is useless here, so switching asks for new credentials.
        keep_saved = gateway == self.saved_gateway
        secret = data.get("secret_key") or (self.instance.secret_key if keep_saved else "")
        token = data.get("webhook_token") or (self.instance.webhook_token if keep_saved else "")
        if gateway == "doku" and not (data.get("doku_client_id") and secret):
            self.add_error("secret_key", "DOKU membutuhkan Client ID dan Secret Key.")
        if gateway == "xendit" and not (secret and token):
            self.add_error("secret_key", "Xendit membutuhkan Secret API Key dan token verifikasi webhook.")
        return data

    def save(self, commit=True):
        account = super().save(commit=False)
        if not account.gateway:
            account.doku_client_id, account.secret_key, account.webhook_token = "", "", ""
        else:
            if account.gateway != self.saved_gateway:
                account.secret_key, account.webhook_token = "", ""
            if self.cleaned_data.get("secret_key"):
                account.secret_key = self.cleaned_data["secret_key"]
            if self.cleaned_data.get("webhook_token"):
                account.webhook_token = self.cleaned_data["webhook_token"]
            if account.gateway == "xendit":
                account.doku_client_id = ""
        if commit:
            account.save()
        return account
