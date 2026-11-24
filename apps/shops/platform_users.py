"""Platform users: what the partner (superuser) does with accounts. Add a person (optionally straight into a
company with a role), switch an account off or on, delete one that never did anything.

Deleting is refused while the person owns a company or organization or signed off a cash register, because
those records must keep their author. Switching the account off is the normal way to remove access."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ProtectedError, Q

from . import platform_log
from .auth import login_phone
from .models import AuditLog, Store, StoreStaff


class PlatformUserError(Exception):
    """A refusal the page shows as a message."""


def contact_of(value):
    """Normalise what the partner typed: an email (lower case) or an Indonesian WhatsApp number (08…)."""
    value = (value or "").strip()
    if "@" in value:
        local, _, domain = value.partition("@")
        if not local or "." not in domain or " " in value:
            raise PlatformUserError("Email tidak valid.")
        return value.lower()
    phone = login_phone(value)
    if not phone:
        raise PlatformUserError("Tulis email atau nomor WhatsApp Indonesia, mis. 081234567890.")
    return phone


def search(query=""):
    """Users with their companies, newest first. The query matches name, login, email."""
    User = get_user_model()
    users = User.objects.all().order_by("-date_joined")
    query = (query or "").strip()
    if query:
        users = users.filter(Q(username__icontains=query) | Q(email__icontains=query)
            | Q(first_name__icontains=query) | Q(last_name__icontains=query))
    users = list(users[:200])
    owned, member = {}, {}
    for store in Store.objects.filter(owner__in=users).only("id", "name", "owner_id"):
        owned.setdefault(store.owner_id, []).append(store)
    for row in StoreStaff.objects.filter(user__in=users).select_related("store").only(
            "user_id", "role", "store__name"):
        member.setdefault(row.user_id, []).append(row)
    for user in users:
        user.owned_stores = owned.get(user.id, [])
        user.memberships = member.get(user.id, [])
    return users


@transaction.atomic
def create_user(*, name, contact, store=None, role="", actor=None):
    """Create the account without a password and return ``(user, created, invite_link)``. With ``store`` and
    ``role`` the person is added to that company. An existing account is reused, never overwritten."""
    from .staff_views import invite_link

    name = (name or "").strip()
    if not name:
        raise PlatformUserError("Tulis nama orangnya.")
    contact = contact_of(contact)
    if store is not None and role not in dict(StoreStaff._meta.get_field("role").choices):
        raise PlatformUserError("Pilih peran untuk perusahaan itu.")
    User = get_user_model()
    user = (User.objects.filter(username__iexact=contact).first()
        or (User.objects.filter(email__iexact=contact).first() if "@" in contact else None))
    created = user is None
    if created:
        user = User(username=contact, email=contact if "@" in contact else "", first_name=name[:150])
        user.set_unusable_password()
        user.save()
    if store is not None:
        if user.id == store.owner_id:
            raise PlatformUserError("Itu akun pemilik perusahaan.")
        StoreStaff.objects.update_or_create(store=store, user=user, defaults={"role": role})
        AuditLog.objects.create(store=store, actor=actor, action="staff.invited", object_type="User",
            object_id=str(user.id), details={"role": role, "new": str(created), "by": "platform"})
    link = invite_link(user) if not user.has_usable_password() else None
    platform_log.log(actor, "user.created" if created else "user.added_to_company", "User", user.username,
        {"nama": ["", name], "perusahaan": ["", store.name if store else ""], "peran": ["", role if store else ""]},
        store=store)
    return user, created, link


def _check_target(user, actor):
    if user.id == actor.id:
        raise PlatformUserError("Anda tidak bisa mengubah akun Anda sendiri.")
    if user.is_superuser:
        raise PlatformUserError("Akun partner platform dikelola lewat server, bukan dari halaman ini.")


def set_active(user, active, actor):
    _check_target(user, actor)
    user.is_active = bool(active)
    user.save(update_fields=["is_active"])
    platform_log.log(actor, "user.activated" if active else "user.deactivated", "User", user.username,
        {"aktif": ["tidak" if active else "ya", "ya" if active else "tidak"]})
    return user


def delete_user(user, actor):
    """Remove an account that owns nothing. Memberships go with it; anything the person authored that must be
    kept makes the delete refuse, and the page offers to switch the account off instead."""
    _check_target(user, actor)
    owned = list(Store.objects.filter(owner=user).values_list("name", flat=True))
    if owned:
        raise PlatformUserError("Akun ini pemilik " + ", ".join(owned) + ". Matikan akunnya, jangan dihapus.")
    label = user.get_full_name() or user.username
    try:
        with transaction.atomic():
            user.delete()
    except ProtectedError:
        raise PlatformUserError(f"{label} sudah tercatat di data perusahaan (organisasi atau shift kasir). "
            "Matikan akunnya agar tidak bisa masuk, data tetap utuh.") from None
    platform_log.log(actor, "user.deleted", "User", user.username or label)
    return label


@transaction.atomic
def update_user(user, *, name, contact, actor):
    """Change a person's name and login (email or WhatsApp). The login must not belong to anyone else."""
    _check_target(user, actor)
    name = (name or "").strip()
    if not name:
        raise PlatformUserError("Tulis nama orangnya.")
    contact = contact_of(contact)
    User = get_user_model()
    taken = User.objects.exclude(pk=user.pk).filter(Q(username__iexact=contact) | Q(email__iexact=contact))
    if taken.exists():
        raise PlatformUserError("Email atau nomor itu sudah dipakai akun lain.")
    before = {"nama": user.get_full_name() or user.first_name, "login": user.username}
    user.first_name, user.last_name = name[:150], ""
    user.username = contact
    user.email = contact if "@" in contact else ""
    user.save(update_fields=["first_name", "last_name", "username", "email"])
    platform_log.log(actor, "user.updated", "User", user.username,
        platform_log.diff(before, {"nama": name[:150], "login": contact}))
    return user


def set_membership(user, store, role, actor):
    """Put the person in a company with a role, or change the role they already have."""
    _check_target(user, actor)
    if role not in dict(StoreStaff._meta.get_field("role").choices):
        raise PlatformUserError("Pilih peran.")
    if user.id == store.owner_id:
        raise PlatformUserError("Itu akun pemilik perusahaan.")
    old = StoreStaff.objects.filter(store=store, user=user).values_list("role", flat=True).first() or ""
    row, _ = StoreStaff.objects.update_or_create(store=store, user=user, defaults={"role": role})
    platform_log.log(actor, "user.role_changed", "User", user.username, {"peran": [old, role]}, store=store)
    AuditLog.objects.create(store=store, actor=actor, action="staff.role_changed", object_type="User",
        object_id=str(user.id), details={"role": role, "by": "platform"})
    return row


def remove_membership(user, store, actor):
    _check_target(user, actor)
    removed, _ = StoreStaff.objects.filter(store=store, user=user).delete()
    if removed:
        platform_log.log(actor, "user.removed_from_company", "User", user.username, {"perusahaan": [store.name, ""]},
            store=store)
        AuditLog.objects.create(store=store, actor=actor, action="staff.removed", object_type="User",
            object_id=str(user.id), details={"by": "platform"})
    return bool(removed)


def password_link(user, actor):
    """A fresh one-time link to set a password: for a new person, or someone who forgot theirs."""
    from .staff_views import invite_link

    _check_target(user, actor)
    platform_log.log(actor, "user.password_link", "User", user.username)
    return invite_link(user)
