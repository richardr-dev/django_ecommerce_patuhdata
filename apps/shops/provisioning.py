"""Instalasi toko oleh partner: the only way a store comes to exist.

There is no public registration. A partner installs the system for a client with
``manage.py install_store`` or the superuser-only page ``/instalasi/``; both call ``install`` here.
One call leaves a store that can keep books from its first minute: the owner's account (with a
one-time link to set a password), the Organization, the store, its main warehouse and shipping
method, the chart of accounts with Kas utama, and the tax profile. A second brand for the same
client joins the client's Organization (``organization=``)."""
import re

from django.contrib.auth import get_user_model
from django.db import transaction

from .models import AuditLog, Organization, Store
from .tenancy import RESERVED_SLUGS

SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])")


class InstallError(ValueError):
    pass


def clean_slug(value):
    slug = (value or "").strip().lower()
    if not SLUG_PATTERN.fullmatch(slug) or "--" in slug:
        raise InstallError("Kode toko: 3–40 huruf kecil, angka, atau tanda hubung (tidak di awal/akhir).")
    if slug in RESERVED_SLUGS or Store.objects.filter(slug=slug).exists():
        raise InstallError(f"Kode toko “{slug}” sudah dipakai.")
    return slug


def _owner(email, phone, name):
    """The client's account: found by its email or WhatsApp number, else created without a password
    (the invite link sets it). Returns ``(user, created)``."""
    User = get_user_model()
    login_name = email or phone
    user = (User.objects.filter(username__iexact=login_name).first()
        or (User.objects.filter(email__iexact=email).first() if email else None))
    if user:
        return user, False
    user = User(username=login_name, email=email, first_name=name[:150])
    user.set_unusable_password()
    user.save()
    return user, True


@transaction.atomic
def install(*, store_name, slug, owner_name, owner_email="", owner_phone="", legal_name="", npwp="",
        address="", is_pkp=False, taxpayer_type="company", organization=None, plan=None, installed_by=None):
    """Create a client's store, ready to use. Returns ``{"store", "owner", "owner_created", "invite_link"}``;
    ``invite_link`` is the one-time link for the owner to set a password (None when the account already
    had one)."""
    from apps.accounting.bank import bank_accounts
    from apps.accounting.ledger import chart_of_accounts
    from apps.billing.models import Subscription
    from apps.billing.plans import trial_end_for
    from apps.orders.services import normalize_indonesian_phone
    from apps.orders.setup_views import create_store_defaults
    from apps.tax.models import TaxProfile
    from apps.tax.services import normalize_npwp

    from .staff_views import invite_link

    store_name, owner_name = (store_name or "").strip(), (owner_name or "").strip()
    if not store_name:
        raise InstallError("Nama toko wajib diisi.")
    if not owner_name:
        raise InstallError("Nama pemilik wajib diisi.")
    owner_email = (owner_email or "").strip().lower()
    try:
        owner_phone = normalize_indonesian_phone(owner_phone) if (owner_phone or "").strip() else ""
        npwp = normalize_npwp(npwp)
    except ValueError as error:
        raise InstallError(str(error)) from None
    if not owner_email and not owner_phone:
        raise InstallError("Isi email atau nomor WhatsApp pemilik: itu yang dipakai untuk masuk.")
    if is_pkp and not npwp:
        raise InstallError("PKP wajib punya NPWP.")
    if taxpayer_type not in dict(TaxProfile.TAXPAYER_TYPES):
        raise InstallError("Jenis wajib pajak tidak dikenal.")
    slug = clean_slug(slug)
    owner, created = _owner(owner_email, owner_phone, owner_name)
    if organization is None:
        org_slug, suffix = slug, 2
        while Organization.objects.filter(slug=org_slug).exists():
            org_slug, suffix = f"{slug[:44]}-{suffix}", suffix + 1
        organization = Organization.objects.create(name=legal_name or store_name, slug=org_slug, owner=owner)
    elif organization.owner_id != owner.id:
        raise InstallError(f"Organisasi {organization.name} milik akun lain. Brand tambahan harus memakai "
            "email atau nomor pemilik yang sama.")
    store = Store.objects.create(owner=owner, organization=organization, name=store_name, slug=slug,
        support_phone=owner_phone, support_email=owner_email)
    if plan is not None:
        Subscription.objects.create(store=store, plan=plan, current_period_end=trial_end_for(plan))
    create_store_defaults(store)
    chart_of_accounts(store)
    bank_accounts(store)
    TaxProfile.objects.create(store=store, legal_name=legal_name or store_name, npwp=npwp, address=address,
        is_pkp=is_pkp, taxpayer_type=taxpayer_type)
    AuditLog.objects.create(store=store, actor=installed_by, action="store.installed", object_type="Store",
        object_id=str(store.id), details={"owner": owner.username, "owner_created": created,
            "organization": organization.slug, "is_pkp": is_pkp})
    return {"store": store, "owner": owner, "owner_created": created,
        "invite_link": None if owner.has_usable_password() else invite_link(owner)}
