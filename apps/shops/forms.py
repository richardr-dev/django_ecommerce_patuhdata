import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from apps.orders.services import normalize_indonesian_phone

from .models import Store
from .tenancy import RESERVED_SLUGS

SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])")
INSTAGRAM_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}")


def clean_instagram_handle(value):
    """Accept "@toko", "toko" or a profile URL; store the bare username."""
    handle = (value or "").strip()
    for prefix in ("https://", "http://", "www.", "instagram.com/", "@"):
        handle = handle.removeprefix(prefix)
    handle = handle.split("?")[0].strip("/")
    if handle and not INSTAGRAM_PATTERN.fullmatch(handle):
        raise forms.ValidationError("Masukkan nama pengguna Instagram, misalnya batiksari.id.")
    return handle


class StoreSignupForm(forms.Form):
    """Open a store; visitors without an account create one in the same step."""
    store_name = forms.CharField(label="Nama toko", max_length=120)
    slug = forms.CharField(label="Alamat toko", max_length=40,
        help_text="3–40 huruf kecil, angka, atau tanda hubung (-).")
    phone = forms.CharField(label="Nomor WhatsApp toko", max_length=30,
        help_text="Pembeli bisa langsung chat ke nomor ini dari toko Anda.")
    instagram = forms.CharField(label="Instagram toko (opsional)", max_length=60, required=False,
        help_text="Nama pengguna tanpa @, misalnya batiksari.id.")
    full_name = forms.CharField(label="Nama lengkap", max_length=150)
    email = forms.EmailField(label="Email")
    password1 = forms.CharField(label="Kata sandi", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="Ulangi kata sandi", strip=False, widget=forms.PasswordInput)
    accept_terms = forms.BooleanField(label="Saya menyetujui syarat layanan platform dan "
        "bertanggung jawab atas produk yang saya jual.")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user if user and user.is_authenticated else None
        if self.user:
            for name in ("full_name", "email", "password1", "password2"):
                self.fields.pop(name)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "control")
        if "email" in self.fields:
            self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.fields["slug"].widget.attrs.update({"autocapitalize": "none", "spellcheck": "false",
            "placeholder": "nama-toko", "autocomplete": "off"})

    def clean_slug(self):
        slug = self.cleaned_data["slug"].strip().lower()
        if not SLUG_PATTERN.fullmatch(slug) or "--" in slug:
            raise forms.ValidationError("Gunakan 3–40 huruf kecil, angka, atau tanda hubung "
                "(tidak di awal/akhir).")
        if slug in RESERVED_SLUGS or Store.objects.filter(slug=slug).exists():
            raise forms.ValidationError("Alamat ini sudah dipakai. Pilih alamat lain.")
        return slug

    def clean_instagram(self):
        return clean_instagram_handle(self.cleaned_data.get("instagram"))

    def clean_phone(self):
        try:
            return normalize_indonesian_phone(self.cleaned_data["phone"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("Email ini sudah terdaftar. Masuk dulu untuk membuka toko baru.")
        return email

    def clean(self):
        cleaned = super().clean()
        if self.user or not cleaned.get("password1"):
            return cleaned
        if cleaned["password1"] != cleaned.get("password2"):
            self.add_error("password2", "Kata sandi tidak sama.")
            return cleaned
        candidate = get_user_model()(username=cleaned.get("email", ""), email=cleaned.get("email", ""),
            first_name=cleaned.get("full_name", ""))
        try:
            validate_password(cleaned["password1"], candidate)
        except forms.ValidationError as error:
            self.add_error("password1", error)
        return cleaned
