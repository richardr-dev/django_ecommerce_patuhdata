"""/registration/: the partner's page for installing a client's store. Platform superusers only; a store
account is sent back to its dashboard with a message."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect, render

from .models import Organization, Store
from .provisioning import InstallError, install
from .tenancy import platform_url

FIELDS = ("store_name", "slug", "owner_name", "owner_email", "owner_phone", "legal_name", "npwp", "address",
    "taxpayer_type", "organization", "trial_days")


def _email_owner(result):
    """Send the new owner the one-time password link. Returns True only when a real mail backend took it;
    the link is always shown on screen too, so a failed send never blocks the partner."""
    owner, link = result["owner"], result["invite_link"]
    if not (link and owner.email) or "console" in settings.EMAIL_BACKEND:
        return False
    store = result["store"]
    try:
        send_mail(f"Akun {store.name} di {settings.PLATFORM_NAME}",
            f"Halo {owner.first_name or owner.get_full_name() or ''},\n\nToko {store.name} sudah siap di "
            f"{settings.PLATFORM_NAME}. Buat kata sandi Anda lewat link ini (sekali pakai):\n{link}\n\n"
            f"Setelah itu masuk di {platform_url('/login/')} dengan email ini.\n\n{settings.PLATFORM_NAME}",
            settings.DEFAULT_FROM_EMAIL, [owner.email])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("owner invite email failed")
        return False
    return True


@login_required(login_url="/login/")
def partner_install(request):
    if not request.user.is_superuser:
        messages.error(request, "Instalasi perusahaan hanya bisa dilakukan oleh akun partner platform. "
            "Anda masuk sebagai akun toko.")
        return redirect("merchant-dashboard")
    result, error = None, ""
    from . import trial
    if request.method == "POST" and request.POST.get("trial_action"):
        company = Store.objects.filter(pk=request.POST.get("store") or 0).first()
        action = request.POST["trial_action"]
        if company is None:
            messages.error(request, "Perusahaan tidak ditemukan.")
        elif action == "extend":
            trial.extend_trial(company, int(request.POST.get("days") or 14), request.user)
            messages.success(request, f"Uji coba {company.name} diperpanjang.")
        elif action == "activate":
            trial.activate_full_access(company, request.user)
            messages.success(request, f"{company.name} sekarang punya akses penuh.")
        elif action == "end":
            trial.end_trial_now(company, request.user)
            messages.success(request, f"Uji coba {company.name} diakhiri.")
        return redirect("partner-install")
    values = {name: request.POST.get(name, "") for name in FIELDS}
    values["is_pkp"] = request.POST.get("is_pkp") == "1"
    if request.method == "POST":
        organization = Organization.objects.filter(pk=values["organization"] or 0).first()
        try:
            result = install(store_name=values["store_name"], slug=values["slug"], owner_name=values["owner_name"],
                owner_email=values["owner_email"], owner_phone=values["owner_phone"],
                legal_name=values["legal_name"], npwp=values["npwp"], address=values["address"],
                is_pkp=values["is_pkp"], taxpayer_type=values["taxpayer_type"] or "company",
                organization=organization, installed_by=request.user,
                trial_days=int(values["trial_days"]) if values["trial_days"].isdigit() else None)
        except InstallError as problem:
            error = str(problem)
        else:
            result["emailed"] = _email_owner(result)
            values = dict.fromkeys(FIELDS, "") | {"is_pkp": False}
    stores = list(Store.objects.select_related("owner", "organization").order_by("-id")[:100])
    for company in stores:
        company.trial = trial.trial_state(company)
    return render(request, "platform/install.html", {"v": values, "result": result, "error": error,
        "stores": stores, "organizations": Organization.objects.select_related("owner").order_by("name")})


def _email_invite(user, link):
    """Mail the one-time password link; the link is also shown on screen, so a failed send blocks nothing."""
    if not (link and user.email) or "console" in settings.EMAIL_BACKEND:
        return False
    try:
        send_mail(f"Akun Anda di {settings.PLATFORM_NAME}",
            f"Halo {user.first_name or ''},\n\nAkun {settings.PLATFORM_NAME} Anda sudah dibuat. Buat kata sandi "
            f"lewat link ini (sekali pakai):\n{link}\n\nSetelah itu masuk di {platform_url('/login/')} "
            f"dengan email ini.\n\n{settings.PLATFORM_NAME}",
            settings.DEFAULT_FROM_EMAIL, [user.email])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("platform user invite email failed")
        return False
    return True


@login_required(login_url="/login/")
def platform_users(request):
    """/platform/users/: the partner's list of accounts. Add, switch off or on, delete."""
    from django.contrib.auth import get_user_model

    from . import platform_users as service
    from .models import StoreStaff

    if not request.user.is_superuser:
        messages.error(request, "Halaman pengguna hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    invite, error = None, ""
    values = {"name": "", "contact": "", "store": "", "role": ""}
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "create":
                values = {k: request.POST.get(k, "").strip() for k in values}
                store = Store.objects.filter(pk=values["store"]).first() if values["store"] else None
                if values["store"] and store is None:
                    raise service.PlatformUserError("Perusahaan tidak ditemukan.")
                user, created, link = service.create_user(name=values["name"], contact=values["contact"],
                    store=store, role=values["role"], actor=request.user)
                invite = {"user": user, "link": link, "created": created,
                    "emailed": _email_invite(user, link), "store": store}
                values = dict.fromkeys(values, "")
            else:
                target = get_user_model().objects.filter(pk=request.POST.get("user") or 0).first()
                if target is None:
                    raise service.PlatformUserError("Akun tidak ditemukan.")
                if action in ("deactivate", "activate"):
                    service.set_active(target, action == "activate", request.user)
                    messages.success(request, f"{target.get_full_name() or target.username} "
                        f"{'diaktifkan' if action == 'activate' else 'dimatikan; ia tidak bisa masuk lagi'}.")
                elif action == "delete":
                    label = service.delete_user(target, request.user)
                    messages.success(request, f"{label} dihapus.")
                else:
                    raise service.PlatformUserError("Tindakan tidak dikenal.")
                return redirect(request.get_full_path())
        except service.PlatformUserError as problem:
            error = str(problem)
    query = request.GET.get("q", "")
    return render(request, "platform/users.html", {"users": service.search(query), "q": query, "v": values,
        "invite": invite, "error": error, "stores": Store.objects.order_by("name")[:300],
        "roles": StoreStaff._meta.get_field("role").choices})


@login_required(login_url="/login/")
def platform_user_detail(request, user_id):
    """/platform/users/<id>/: edit one account: name and login, companies and roles, a new password link."""
    from django.contrib.auth import get_user_model
    from django.shortcuts import get_object_or_404

    from . import platform_users as service
    from .models import StoreStaff

    if not request.user.is_superuser:
        messages.error(request, "Halaman pengguna hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    person = get_object_or_404(get_user_model(), pk=user_id)
    link, error = None, ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "save":
                service.update_user(person, name=request.POST.get("name", ""),
                    contact=request.POST.get("contact", ""), actor=request.user)
                messages.success(request, "Data pengguna disimpan.")
            elif action in ("role", "remove"):
                store = get_object_or_404(Store, pk=request.POST.get("store") or 0)
                if action == "role":
                    service.set_membership(person, store, request.POST.get("role", ""), request.user)
                    messages.success(request, f"Peran di {store.name} disimpan.")
                else:
                    service.remove_membership(person, store, request.user)
                    messages.success(request, f"Dikeluarkan dari {store.name}.")
            elif action == "link":
                link = service.password_link(person, request.user)
            else:
                raise service.PlatformUserError("Tindakan tidak dikenal.")
            if link is None:
                return redirect("platform-user", user_id=person.pk)
        except service.PlatformUserError as problem:
            error = str(problem)
    return render(request, "platform/user_detail.html", {"person": person, "link": link, "error": error,
        "owned": Store.objects.filter(owner=person), "memberships": StoreStaff.objects.filter(
            user=person).select_related("store").order_by("store__name"),
        "stores": Store.objects.order_by("name")[:300],
        "roles": StoreStaff._meta.get_field("role").choices,
        "locked": person.is_superuser or person.pk == request.user.pk})
