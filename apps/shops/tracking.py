"""Pelacakan & pixel: the seller's own analytics and ad pixels on their storefront.

Only IDs are stored, each checked against its format, so a seller cannot paste a script into
the page. Pixels load only after the shopper picks "Terima semua" on the cookie banner."""
import json
import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.safestring import mark_safe

from .models import AuditLog, Store

FORMATS = {
    "ga4_id": (re.compile(r"^G-[A-Z0-9]{4,15}$"), "Contoh: G-AB12CD34EF"),
    "gtm_id": (re.compile(r"^GTM-[A-Z0-9]{4,12}$"), "Contoh: GTM-AB12CD3"),
    "google_ads_id": (re.compile(r"^AW-\d{6,15}$"), "Contoh: AW-123456789"),
    "meta_pixel_id": (re.compile(r"^\d{10,20}$"), "Angka 15–16 digit dari Events Manager"),
    "tiktok_pixel_id": (re.compile(r"^[A-Z0-9]{15,25}$"), "Contoh: CABC123DEF456GHI789J"),
    "clarity_id": (re.compile(r"^[a-z0-9]{6,20}$"), "Contoh: abcd1234ef"),
    "google_site_verification": (re.compile(r"^[A-Za-z0-9_-]{20,100}$"),
        "Isi content dari tag meta google-site-verification"),
}
CONTENT = re.compile(r'content=["\']([^"\']+)["\']')


class TrackingForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = tuple(FORMATS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Room for the whole <meta> tag Search Console shows; only its content is stored.
        self.fields["google_site_verification"] = forms.CharField(required=False, max_length=300,
            label="Google Search Console")
        for name, (_, hint) in FORMATS.items():
            self.fields[name].help_text = hint
            self.fields[name].widget.attrs.update({"class": "control", "autocomplete": "off",
                "spellcheck": "false"})

    def clean(self):
        cleaned = super().clean()
        for name, (pattern, hint) in FORMATS.items():
            value = (cleaned.get(name) or "").strip()
            if name == "google_site_verification" and "content=" in value:
                found = CONTENT.search(value)  # the whole <meta> tag was pasted
                value = found.group(1) if found else value
            elif name in {"ga4_id", "gtm_id", "google_ads_id", "tiktok_pixel_id"}:
                value = value.upper()
            elif name == "clarity_id":
                value = value.lower()
            if value and not pattern.fullmatch(value):
                self.add_error(name, f"Format tidak dikenali. {hint}.")
            cleaned[name] = value
        return cleaned


def tracking_config(store):
    """The IDs the storefront script needs, as JSON safe to embed in a <script> tag."""
    if store is None:
        return ""
    config = {key: getattr(store, key) for key in FORMATS if key != "google_site_verification"
        and getattr(store, key)}
    if not config:
        return ""
    return mark_safe(json.dumps(config).replace("<", "\\u003c"))


@login_required(login_url="/merchant/login/")
def merchant_tracking(request):
    from apps.orders.views import _merchant_context, _require_editor, _staff_store

    store = _staff_store(request)
    form = TrackingForm(request.POST or None, instance=store)
    if request.method == "POST":
        _require_editor(request, store)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(store=store, actor=request.user, action="store.tracking_updated",
                object_type="Store", object_id=str(store.id),
                details={"fields": list(form.changed_data)})
            messages.success(request, "Pelacakan disimpan. Pixel aktif untuk pengunjung yang "
                "menyetujui cookie.")
            return redirect("merchant-tracking")
    return render(request, "merchant/tracking.html", _merchant_context(request, store,
        title="Pelacakan & pixel", section="tracking", form=form))
