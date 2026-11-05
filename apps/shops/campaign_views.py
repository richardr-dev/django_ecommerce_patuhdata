"""Link kampanye: a seller's own short links (lakuin.store/s/<code>) to promote their store,
with clicks, orders and revenue per link. Inviting sellers to Lakuin is Ajak teman instead
(Langganan page)."""
import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .models import AuditLog, CampaignLink
from .shortlinks import campaign_short_url, new_code

CODE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
RESERVED_CODES = {"admin", "api", "lakuin", "login", "merchant", "start", "static", "media"}
# Orders that are real sales, as counted on the dashboard.
UNSOLD = ["pending", "payment_failed", "cancelled"]


class CampaignLinkForm(forms.Form):
    TARGETS = [("home", "Beranda toko"), ("product", "Halaman produk"), ("path", "Alamat lain di toko")]
    name = forms.CharField(label="Nama kampanye", max_length=80,
        widget=forms.TextInput(attrs={"placeholder": "Contoh: Bio Instagram, Promo Lebaran"}))
    target = forms.ChoiceField(label="Buka halaman", choices=TARGETS, initial="home")
    product_sku = forms.CharField(label="SKU produk", max_length=80, required=False)
    path = forms.CharField(label="Alamat halaman", max_length=300, required=False,
        widget=forms.TextInput(attrs={"placeholder": "/pages/tentang-kami/"}))
    code = forms.CharField(label="Kode link (opsional)", max_length=40, required=False,
        help_text="Huruf kecil, angka, tanda hubung. Kosongkan untuk kode acak.",
        widget=forms.TextInput(attrs={"placeholder": "lebaran26"}))

    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip().lower()
        if not code:
            return ""
        if not CODE.fullmatch(code) or "--" in code or code in RESERVED_CODES:
            raise forms.ValidationError("Gunakan 3–40 huruf kecil, angka, atau tanda hubung.")
        if CampaignLink.objects.filter(code=code).exists():
            raise forms.ValidationError("Kode ini sudah dipakai. Pilih kode lain.")
        return code

    def clean(self):
        cleaned = super().clean()
        target = cleaned.get("target")
        if target == "product":
            from apps.catalog.models import Product
            product = Product.objects.filter(store=self.store,
                sku=(cleaned.get("product_sku") or "").strip()).first()
            if product is None:
                self.add_error("product_sku", "Produk dengan SKU ini tidak ditemukan di toko ini.")
            else:
                cleaned["destination"] = f"/products/{product.slug}/"
        elif target == "path":
            path = (cleaned.get("path") or "").strip()
            if not path.startswith("/") or path.startswith("//") or "://" in path:
                self.add_error("path", "Tulis alamat di toko ini, diawali /, mis. /pages/promo/.")
            else:
                cleaned["destination"] = path
        else:
            cleaned["destination"] = "/"
        return cleaned


def _links(store):
    return (CampaignLink.objects.filter(store=store)
        .annotate(order_count=Count("orders", filter=~Q(orders__status__in=UNSOLD)),
            revenue=Sum("orders__total", filter=~Q(orders__status__in=UNSOLD)))
        .order_by("-created_at"))


@login_required(login_url="/merchant/login/")
def campaign_links(request):
    from apps.orders.views import _merchant_context, _require_editor, _staff_store

    store = _staff_store(request)
    form = CampaignLinkForm(request.POST or None, store=store)
    if request.method == "POST":
        _require_editor(request, store)
        if form.is_valid():
            link = CampaignLink.objects.create(store=store, name=form.cleaned_data["name"],
                code=form.cleaned_data["code"] or new_code(),
                destination=form.cleaned_data["destination"])
            AuditLog.objects.create(store=store, actor=request.user, action="campaign_link.created",
                object_type="CampaignLink", object_id=str(link.id),
                details={"code": link.code, "destination": link.destination})
            messages.success(request, f"Link kampanye dibuat: {campaign_short_url(link)}")
            return redirect("merchant-campaign-links")
    links = [{"link": link, "short_url": campaign_short_url(link)} for link in _links(store)]
    return render(request, "merchant/campaign_links.html", _merchant_context(request, store,
        title="Link kampanye", section="campaign-links", form=form, links=links))


@login_required(login_url="/merchant/login/")
def toggle_campaign_link(request, link_id):
    from apps.orders.views import _require_editor, _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_editor(request, store)
    link = get_object_or_404(CampaignLink, id=link_id, store=store)
    link.is_active = not link.is_active
    link.save(update_fields=["is_active"])
    messages.success(request, f"Link {link.name} {'diaktifkan' if link.is_active else 'dinonaktifkan'}.")
    return redirect("merchant-campaign-links")
