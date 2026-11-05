"""Staf: the owner (or a store admin) invites people by email or WhatsApp number with a role,
so nobody shares the owner's password. A new person gets a one-time link to set their
password (Django's password-reset token: single use, expires after PASSWORD_RESET_TIMEOUT);
someone who already has a Lakuin account is simply added to the store."""
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .auth import login_phone, user_for_phone
from .models import AuditLog, StoreStaff
from .tenancy import platform_url

ROLE_HELP = {
    "admin": "Semua menu, termasuk staf, pembayaran, dan langganan.",
    "manager": "Produk, pesanan, promosi, dan tampilan toko.",
    "ops": "Pesanan, pengiriman, dan stok.",
    "warehouse": "Stok, gudang, dan pengemasan.",
    "finance": "Laporan keuangan, pencairan, pajak, dan langganan.",
    "customer_service": "Pesanan dan pelanggan, termasuk membuat pesanan manual.",
    "viewer": "Hanya melihat, tanpa mengubah apa pun.",
}


class InviteForm(forms.Form):
    name = forms.CharField(label="Nama", max_length=150)
    contact = forms.CharField(label="Email atau nomor WhatsApp", max_length=254,
        widget=forms.TextInput(attrs={"placeholder": "nama@email.com atau 0812…"}))
    role = forms.ChoiceField(label="Peran", choices=StoreStaff._meta.get_field("role").choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "control")

    def clean_contact(self):
        value = self.cleaned_data["contact"].strip()
        if "@" in value:
            try:
                forms.EmailField().clean(value)
            except forms.ValidationError:
                raise forms.ValidationError("Email tidak valid.") from None
            return value.lower()
        phone = login_phone(value)
        if not phone:
            raise forms.ValidationError("Tulis email atau nomor WhatsApp Indonesia, mis. 081234567890.")
        return phone


def _can_manage(request, store):
    if store.owner_id == request.user.id:
        return True
    return store.staff_members.filter(user=request.user, role="admin").exists()


def _find_user(contact):
    User = get_user_model()
    if "@" in contact:
        return (User.objects.filter(username__iexact=contact).first()
            or User.objects.filter(email__iexact=contact).first())
    return user_for_phone(contact)


def invite_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return platform_url(reverse("staff-invite-accept", args=[uid, default_token_generator.make_token(user)]))


@login_required(login_url="/merchant/login/")
def merchant_staff(request):
    from apps.orders.views import _merchant_context, _staff_store

    store = _staff_store(request)
    if not _can_manage(request, store):
        raise Http404
    form = InviteForm(request.POST or None)
    invite = None
    if request.method == "POST" and form.is_valid():
        contact, role = form.cleaned_data["contact"], form.cleaned_data["role"]
        user = _find_user(contact)
        created = user is None
        if created:
            User = get_user_model()
            user = User(username=contact, email=contact if "@" in contact else "",
                first_name=form.cleaned_data["name"][:150])
            user.set_unusable_password()
            user.save()
        if user.id == store.owner_id:
            messages.info(request, "Itu akun pemilik toko.")
            return redirect("merchant-staff")
        StoreStaff.objects.update_or_create(store=store, user=user, defaults={"role": role})
        AuditLog.objects.create(store=store, actor=request.user, action="staff.invited",
            object_type="User", object_id=str(user.id), details={"role": role, "new": created})
        if created or not user.has_usable_password():
            invite = {"name": form.cleaned_data["name"], "link": invite_link(user),
                "contact": contact, "email": "@" in contact}
            if invite["email"]:
                _email_invite(store, user, invite["link"])
            messages.success(request, f"{form.cleaned_data['name']} diundang. Kirim link di bawah "
                "agar ia membuat kata sandi.")
        else:
            messages.success(request, f"{user.get_full_name() or user.username} ditambahkan sebagai "
                f"{dict(InviteForm.base_fields['role'].choices)[role]}. Ia masuk dengan akunnya sendiri.")
        if not invite:
            return redirect("merchant-staff")
        form = InviteForm()
    staff = store.staff_members.select_related("user").order_by("user__first_name", "user__username")
    return render(request, "merchant/staff.html", _merchant_context(request, store,
        title="Staf", section="staff", form=form, staff=staff, invite=invite,
        roles=[(code, label, ROLE_HELP.get(code, "")) for code, label in
            StoreStaff._meta.get_field("role").choices]))


def _email_invite(store, user, link):
    from django.conf import settings
    try:
        send_mail(f"Undangan staf {store.name} di {settings.PLATFORM_NAME}",
            f"Halo {user.first_name or ''},\n\nAnda diundang mengelola toko {store.name}. Buat kata "
            f"sandi Anda lewat link ini (sekali pakai):\n{link}\n\nSetelah itu masuk di "
            f"{platform_url('/merchant/login/')} dengan email ini.\n\n{settings.PLATFORM_NAME}",
            settings.DEFAULT_FROM_EMAIL, [user.email])
    except Exception:  # the invite link is also shown on screen
        import logging
        logging.getLogger(__name__).exception("staff invite email failed")


@login_required(login_url="/merchant/login/")
def merchant_staff_update(request, staff_id):
    from apps.orders.views import _staff_store

    if request.method != "POST":
        raise Http404
    store = _staff_store(request)
    if not _can_manage(request, store):
        raise Http404
    member = get_object_or_404(StoreStaff, id=staff_id, store=store)
    if member.user_id == request.user.id:
        messages.error(request, "Anda tidak bisa mengubah peran Anda sendiri.")
        return redirect("merchant-staff")
    if request.POST.get("action") == "remove":
        member.delete()
        AuditLog.objects.create(store=store, actor=request.user, action="staff.removed",
            object_type="User", object_id=str(member.user_id), details={})
        messages.success(request, "Staf dihapus dari toko ini.")
    elif request.POST.get("action") == "link" and not member.user.has_usable_password():
        messages.success(request, f"Link baru untuk {member.user.get_full_name() or member.user.username}: "
            f"{invite_link(member.user)}")
    elif request.POST.get("role") in ROLE_HELP:
        member.role = request.POST["role"]
        member.save(update_fields=["role"])
        AuditLog.objects.create(store=store, actor=request.user, action="staff.role_changed",
            object_type="User", object_id=str(member.user_id), details={"role": member.role})
        messages.success(request, "Peran diperbarui.")
    return redirect("merchant-staff")


def staff_invite_accept(request, uidb64, token):
    """The invited person sets their password, then lands in the back office."""
    User = get_user_model()
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    valid = user is not None and default_token_generator.check_token(user, token)
    form = SetPasswordForm(user, request.POST or None) if valid else None
    if valid and request.method == "POST" and form.is_valid():
        form.save()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Kata sandi dibuat. Selamat datang!")
        return redirect("merchant-dashboard")
    return render(request, "registration/staff_invite.html", {"form": form, "valid": valid,
        "invited": user if valid else None})
