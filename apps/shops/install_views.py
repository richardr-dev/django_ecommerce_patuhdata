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
    "taxpayer_type", "organization")


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
        messages.error(request, "Instalasi toko hanya bisa dilakukan oleh akun partner platform. "
            "Anda masuk sebagai akun toko.")
        return redirect("merchant-dashboard")
    result, error = None, ""
    values = {name: request.POST.get(name, "") for name in FIELDS}
    values["is_pkp"] = request.POST.get("is_pkp") == "1"
    if request.method == "POST":
        organization = Organization.objects.filter(pk=values["organization"] or 0).first()
        try:
            result = install(store_name=values["store_name"], slug=values["slug"], owner_name=values["owner_name"],
                owner_email=values["owner_email"], owner_phone=values["owner_phone"],
                legal_name=values["legal_name"], npwp=values["npwp"], address=values["address"],
                is_pkp=values["is_pkp"], taxpayer_type=values["taxpayer_type"] or "company",
                organization=organization, installed_by=request.user)
        except InstallError as problem:
            error = str(problem)
        else:
            result["emailed"] = _email_owner(result)
            values = dict.fromkeys(FIELDS, "") | {"is_pkp": False}
    stores = Store.objects.select_related("owner", "organization").order_by("-id")[:100]
    return render(request, "platform/install.html", {"v": values, "result": result, "error": error,
        "stores": stores, "organizations": Organization.objects.select_related("owner").order_by("name")})
