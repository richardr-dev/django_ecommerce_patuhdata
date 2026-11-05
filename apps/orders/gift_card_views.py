"""Back office: issue gift cards and follow their balances."""
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db.models import Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.shops.models import AuditLog
from apps.shops.tenancy import store_url

from .gift_cards import issue
from .models import GiftCard

ROLES = {"admin", "manager", "customer_service"}


class GiftCardForm(forms.Form):
    value = forms.DecimalField(label="Nilai (Rp)", min_value=1000, max_value=100_000_000,
        decimal_places=0)
    recipient_name = forms.CharField(label="Nama penerima (opsional)", max_length=120, required=False)
    recipient_email = forms.EmailField(label="Email penerima (opsional)", required=False,
        help_text="Kode dikirim ke email ini.")
    expires_on = forms.DateField(label="Berlaku sampai (opsional)", required=False,
        widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label="Catatan internal (opsional)", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Contoh: hadiah giveaway IG"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")


def _email(card):
    store = card.store
    try:
        send_mail(f"Gift card {store.name} untuk Anda",
            f"Halo {card.recipient_name or ''},\n\nAnda menerima gift card {store.name} senilai "
            f"Rp{card.initial_value:,.0f}".replace(",", ".") + f".\n\nKode: {card.code}\n"
            + (f"Berlaku sampai {card.expires_on:%d %b %Y}.\n" if card.expires_on else "")
            + f"\nBelanja di {store_url(store)} dan masukkan kode di halaman checkout.\n\n{store.name}",
            settings.DEFAULT_FROM_EMAIL, [card.recipient_email])
    except Exception:  # the code is shown on screen too
        import logging
        logging.getLogger(__name__).exception("gift card email failed")


@login_required(login_url="/merchant/login/")
def merchant_gift_cards(request):
    from .views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, ROLES)
    form = GiftCardForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        card = issue(store, data["value"], actor=request.user, recipient_name=data["recipient_name"],
            recipient_email=data["recipient_email"], note=data["note"], expires_on=data["expires_on"])
        if card.recipient_email:
            _email(card)
        AuditLog.objects.create(store=store, actor=request.user, action="gift_card.created",
            object_type="GiftCard", object_id=str(card.id),
            details={"label": card.masked, "value": str(card.initial_value)})
        messages.success(request, f"Gift card {card.code} dibuat"
            + (f" dan dikirim ke {card.recipient_email}." if card.recipient_email else "."))
        return redirect("merchant-gift-cards")
    cards = GiftCard.objects.filter(store=store).prefetch_related("entries")[:200]
    totals = GiftCard.objects.filter(store=store, is_active=True).aggregate(
        outstanding=Sum("balance"), issued=Sum("initial_value"))
    return render(request, "merchant/gift_cards.html", _merchant_context(request, store,
        title="Gift card", section="gift-cards", form=form, cards=cards, totals=totals))


@login_required(login_url="/merchant/login/")
def merchant_gift_card_toggle(request, card_id):
    from .views import _require_roles, _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, ROLES)
    card = get_object_or_404(GiftCard, id=card_id, store=store)
    card.is_active = not card.is_active
    card.save(update_fields=["is_active"])
    AuditLog.objects.create(store=store, actor=request.user, action="gift_card.updated",
        object_type="GiftCard", object_id=str(card.id),
        details={"label": card.masked, "active": card.is_active})
    messages.success(request, f"Gift card {card.masked} {'diaktifkan' if card.is_active else 'dinonaktifkan'}.")
    return redirect("merchant-gift-cards")
