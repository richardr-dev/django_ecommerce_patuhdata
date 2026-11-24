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


def _trial_action(request):
    """One trial button from a company row: extend, set an exact date, full access, or end now."""
    import datetime

    from django.utils import timezone

    from . import trial
    company = Store.objects.filter(pk=request.POST.get("store") or 0).first()
    action = request.POST.get("trial_action", "")
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
    elif action == "set":
        try:
            day = datetime.date.fromisoformat(request.POST.get("date", ""))
        except ValueError:
            messages.error(request, "Pilih tanggal akhir uji coba.")
        else:
            trial.set_trial_end(company, timezone.make_aware(datetime.datetime.combine(
                day, datetime.time(23, 59, 59))), request.user)
            messages.success(request, f"Uji coba {company.name} berakhir {day:%d-%m-%Y}.")


@login_required(login_url="/login/")
def partner_install(request):
    if not request.user.is_superuser:
        messages.error(request, "Instalasi perusahaan hanya bisa dilakukan oleh akun partner platform. "
            "Anda masuk sebagai akun toko.")
        return redirect("merchant-dashboard")
    result, error = None, ""
    from . import trial
    if request.method == "POST" and request.POST.get("trial_action"):
        _trial_action(request)
        return redirect("partner-install")
    values = {name: request.POST.get(name, "") for name in FIELDS}
    if request.method != "POST":
        from .models import PlatformSettings
        default_days = PlatformSettings.get().default_trial_days
        values["trial_days"] = str(default_days) if default_days else ""
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
            from . import platform_log
            platform_log.log(request.user, "store.installed", "Store", result["store"].name,
                {"pemilik": ["", result["owner"].username], "uji_coba_hari": ["", values["trial_days"]]},
                store=result["store"])
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


@login_required(login_url="/login/")
def platform_company(request, store_id):
    """/platform/companies/<id>/: edit a company's details, tax identity and trial end."""
    from django.shortcuts import get_object_or_404

    from apps.tax.models import TaxProfile

    from . import platform_companies as service
    from .models import StoreStaff

    if not request.user.is_superuser:
        messages.error(request, "Halaman perusahaan hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    store = get_object_or_404(Store.objects.select_related("owner", "organization"), pk=store_id)
    error, sub_error = "", ""
    if request.method == "POST" and request.POST.get("section") == "subscription":
        from apps.billing import platform_subscriptions as subscriptions
        post = request.POST
        try:
            subscriptions.update_subscription(store, plan_code=post.get("plan", ""),
                period_end=post.get("period_end", ""), auto_renew=post.get("auto_renew") == "1",
                renew_months=12 if post.get("renew_months") == "12" else 1,
                extra_outlets=post.get("extra_outlets", "0"), actor=request.user)
        except subscriptions.SubscriptionError as problem:
            sub_error = str(problem)
        else:
            messages.success(request, "Langganan disimpan.")
            return redirect("platform-company", store_id=store.pk)
    elif request.method == "POST":
        post = request.POST
        try:
            service.update_company(store, name=post.get("name", ""), support_email=post.get("support_email", ""),
                support_phone=post.get("support_phone", ""), legal_name=post.get("legal_name", ""),
                npwp=post.get("npwp", ""), address=post.get("address", ""),
                taxpayer_type=post.get("taxpayer_type", "company"), is_pkp=post.get("is_pkp") == "1",
                trial_end=post.get("trial_end", ""), actor=request.user)
        except service.CompanyError as problem:
            error = str(problem)
        else:
            messages.success(request, "Data perusahaan disimpan.")
            return redirect("platform-company", store_id=store.pk)
    from apps.billing.models import Plan

    from . import trial
    return render(request, "platform/company.html", {"store": store, "error": error, "sub_error": sub_error,
        "subscription": getattr(store, "subscription", None), "plans": Plan.objects.order_by("sort_order"),
        "logs": store.platform_logs.all()[:15],
        "profile": TaxProfile.objects.filter(store=store).first(), "trial": trial.trial_state(store),
        "staff": StoreStaff.objects.filter(store=store).select_related("user").order_by("user__username"),
        "post": request.POST if (error or sub_error) else None})


@login_required(login_url="/login/")
def platform_settings(request):
    """/platform/settings/: the sales contact shown when a trial ends, and the default trial length."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from .models import PlatformSettings

    if not request.user.is_superuser:
        messages.error(request, "Pengaturan platform hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    conf, error = PlatformSettings.get(), ""
    if request.method == "POST":
        email, days = request.POST.get("sales_email", "").strip(), request.POST.get("default_trial_days", "").strip()
        try:
            if email:
                validate_email(email)
            if days and not days.isdigit():
                raise ValidationError("x")
        except ValidationError:
            error = "Periksa email sales dan lama uji coba (angka hari)."
        else:
            from . import platform_log
            old = {"whatsapp_sales": conf.sales_whatsapp, "email_sales": conf.sales_email,
                "uji_coba_bawaan": conf.default_trial_days or ""}
            conf.sales_whatsapp = request.POST.get("sales_whatsapp", "").strip()[:30]
            conf.sales_email = email
            conf.default_trial_days = int(days) if days else None
            conf.save()
            changes = platform_log.diff(old, {"whatsapp_sales": conf.sales_whatsapp, "email_sales": conf.sales_email,
                "uji_coba_bawaan": conf.default_trial_days or ""})
            if changes:
                platform_log.log(request.user, "settings.updated", "PlatformSettings", "Pengaturan platform", changes)
            messages.success(request, "Pengaturan platform disimpan.")
            return redirect("platform-settings")
    return render(request, "platform/settings.html", {"conf": conf, "error": error})


@login_required(login_url="/login/")
def platform_companies(request):
    """/platform/companies/: every company with its trial, and the trial buttons."""
    from . import trial

    if not request.user.is_superuser:
        messages.error(request, "Halaman perusahaan hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    if request.method == "POST":
        _trial_action(request)
        return redirect("platform-companies")
    query = request.GET.get("q", "").strip()
    companies = Store.objects.select_related("owner", "organization").order_by("-id")
    if query:
        companies = companies.filter(name__icontains=query) | companies.filter(slug__icontains=query)
    plan = request.GET.get("plan", "").strip()
    if plan:
        companies = companies.filter(subscription__plan__code=plan)
    companies = list(companies[:200])
    for company in companies:
        company.trial = trial.trial_state(company)
    return render(request, "platform/companies.html", {"companies": companies, "q": query, "plan": plan})


@login_required(login_url="/login/")
def platform_plans(request):
    """/platform/plans/: every plan with its price; add, edit or delete one."""
    from django.db.models import Count

    from apps.billing.models import Plan, Subscription

    if not request.user.is_superuser:
        messages.error(request, "Halaman paket hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    if request.method == "POST" and request.POST.get("action") == "delete":
        from apps.billing import platform_plans as service
        plan = Plan.objects.filter(pk=request.POST.get("plan") or 0).first()
        try:
            if plan is None:
                raise service.PlanError("Paket tidak ditemukan.")
            messages.success(request, f"Paket {service.delete_plan(plan, request.user)} dihapus.")
        except service.PlanError as problem:
            messages.error(request, str(problem))
        return redirect("platform-plans")
    plans = list(Plan.objects.order_by("sort_order", "price_monthly"))
    counts = {row["plan_id"]: row["n"] for row in Subscription.objects.values("plan_id").annotate(
        n=Count("id"))}
    for plan in plans:
        plan.users = counts.get(plan.id, 0)
    return render(request, "platform/plans.html", {"plans": plans})


@login_required(login_url="/login/")
def platform_plan(request, plan_id=None):
    """/platform/plans/new/ and /platform/plans/<id>/: one plan's price, limits and features."""
    from django.shortcuts import get_object_or_404

    from apps.billing import platform_plans as service
    from apps.billing.models import Plan

    if not request.user.is_superuser:
        messages.error(request, "Halaman paket hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    plan = get_object_or_404(Plan, pk=plan_id) if plan_id else None
    error, post = "", None
    if request.method == "POST":
        post = request.POST
        try:
            saved = service.save_plan(plan, post, request.user)
        except service.PlanError as problem:
            error = str(problem)
        else:
            messages.success(request, f"Paket {saved.name} disimpan. Harga baru berlaku untuk tagihan berikutnya.")
            return redirect("platform-plans")
    return render(request, "platform/plan_form.html", {"plan": plan, "error": error, "post": post})


@login_required(login_url="/login/")
def platform_log_page(request):
    """/platform/log/: what the partner changed, from what to what."""
    from .models import PlatformLog

    if not request.user.is_superuser:
        messages.error(request, "Riwayat hanya untuk akun partner platform.")
        return redirect("merchant-dashboard")
    query, kind = request.GET.get("q", "").strip(), request.GET.get("jenis", "").strip()
    entries = PlatformLog.objects.select_related("store")
    if kind:
        entries = entries.filter(action__startswith=kind + ".")
    if query:
        from django.db.models import Q
        entries = entries.filter(Q(target_label__icontains=query) | Q(actor_name__icontains=query)
            | Q(action__icontains=query))
    return render(request, "platform/log.html", {"entries": entries[:300], "q": query, "kind": kind,
        "kinds": [("plan", "Paket & harga"), ("subscription", "Langganan"), ("store", "Perusahaan"),
            ("company", "Data perusahaan"), ("trial", "Uji coba"), ("user", "Pengguna"), ("settings", "Pengaturan")]})
