"""/instalasi/: the partner's page for installing a client's store. Platform superusers only; a store
account is sent back to its dashboard with a message."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import Organization, Store
from .provisioning import InstallError, install

FIELDS = ("store_name", "slug", "owner_name", "owner_email", "owner_phone", "legal_name", "npwp", "address",
    "taxpayer_type", "organization")


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
            values = dict.fromkeys(FIELDS, "") | {"is_pkp": False}
    stores = Store.objects.select_related("owner", "organization").order_by("-id")[:100]
    return render(request, "platform/install.html", {"v": values, "result": result, "error": error,
        "stores": stores, "organizations": Organization.objects.select_related("owner").order_by("name")})
