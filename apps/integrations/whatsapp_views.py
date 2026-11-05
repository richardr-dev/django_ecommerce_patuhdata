"""Pengaturan > WhatsApp: connect the seller's WhatsApp sender, choose which order updates
buyers get, send a test message, and see what was sent."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render

from apps.shops.models import AuditLog

from .forms import WhatsAppSettingsForm
from .http import ProviderError
from .models import StoreWhatsAppSettings, WhatsAppMessage
from .whatsapp import KINDS, msisdn, send


@login_required(login_url="/merchant/login/")
def merchant_whatsapp(request):
    from apps.orders.views import _merchant_context, _require_roles, _staff_store

    store = _staff_store(request)
    _require_roles(request, store, {"admin"})
    account, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    form = WhatsAppSettingsForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(store=store, actor=request.user,
            action="whatsapp.settings_updated", object_type="StoreWhatsAppSettings",
            object_id=str(account.id), details={"provider": account.provider,
                "label": account.get_provider_display()})
        messages.success(request, "Pengaturan WhatsApp disimpan.")
        return redirect("merchant-whatsapp")
    log = list(WhatsAppMessage.objects.filter(store=store).select_related("order")[:50])
    for message in log:
        message.kind_label = KINDS.get(message.kind, message.kind)
    return render(request, "merchant/whatsapp.html", _merchant_context(request, store, form=form,
        account=account, log=log, test_phone=store.support_phone))


@login_required(login_url="/merchant/login/")
def merchant_whatsapp_test(request):
    from apps.orders.views import _require_roles, _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    _require_roles(request, store, {"admin"})
    account = StoreWhatsAppSettings.objects.filter(store=store).first()
    to = msisdn(request.POST.get("phone", ""))
    if not account or not account.ready:
        messages.error(request, "Simpan layanan pengirim dan tokennya terlebih dahulu.")
    elif not to:
        messages.error(request, "Masukkan nomor WhatsApp Indonesia, mis. 081234567890.")
    else:
        try:
            sent = send(account, kind="test", to=to, text=f"Tes notifikasi WhatsApp dari "
                f"{store.name} melalui Lakuin. Jika pesan ini sampai, koneksi sudah benar.")
        except ProviderError as error:
            messages.error(request, f"Pesan tes gagal: {error}")
        else:
            if sent.status == "sent":
                messages.success(request, f"Pesan tes terkirim ke +{to}.")
            else:
                messages.error(request, f"Pesan tes ditolak: {sent.error}")
    return redirect("merchant-whatsapp")
