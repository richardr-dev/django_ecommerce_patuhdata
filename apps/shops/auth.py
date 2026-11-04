"""Sellers sign in with their email or their WhatsApp number. Many sellers have no email,
so an account opened without one uses the WhatsApp number as its login name, and older
accounts can also sign in with the WhatsApp number of the store they own."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core import signing

from apps.orders.services import normalize_indonesian_phone

SELLER_COOKIE = "lakuin_seller"
SELLER_COOKIE_SALT = "lakuin.seller-stores"
SELLER_COOKIE_AGE = 60 * 60 * 24 * 30


def login_phone(value):
    """The 08… form of an Indonesian number, or None when the value is not one."""
    value = (value or "").strip()
    if not value or "@" in value or not any(character.isdigit() for character in value):
        return None
    if any(character.isalpha() for character in value):
        return None
    try:
        return normalize_indonesian_phone(value)
    except ValueError:
        return None


def user_for_phone(phone):
    """The account a WhatsApp number signs in to: the account named after it, else the single
    owner of the store(s) with that number. None when no one, or more than one person, fits."""
    from .models import Store

    User = get_user_model()
    user = User.objects.filter(username=phone).first()
    if user:
        return user
    # Store numbers typed in settings may carry spaces, dashes or +62, so compare normalized.
    owners = {store.owner_id for store in Store.objects.filter(
        support_phone__endswith=phone[-3:]).only("owner_id", "support_phone")
        if login_phone(store.support_phone) == phone}
    return User.objects.filter(id=owners.pop()).first() if len(owners) == 1 else None


class EmailOrPhoneBackend(ModelBackend):
    """Runs after Django's ModelBackend, which already handles exact login names."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or password is None:
            return None
        User = get_user_model()
        phone = login_phone(username)
        if phone:
            user = user_for_phone(phone)
        else:
            matches = list(User.objects.filter(username__iexact=username.strip())[:2]) or list(
                User.objects.filter(email__iexact=username.strip())[:2])
            user = matches[0] if len(matches) == 1 else None
        if user is None:
            User().set_password(password)  # same work as a real check, so timing reveals nothing
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


def seller_store_ids(user):
    from .models import Store

    if not user.is_authenticated:
        return []
    owned = Store.objects.filter(owner=user).values_list("id", flat=True)
    staffed = Store.objects.filter(staff_members__user=user).values_list("id", flat=True)
    return sorted({*owned, *staffed})


def read_seller_cookie(request):
    try:
        return set(signing.loads(request.COOKIES.get(SELLER_COOKIE, ""), salt=SELLER_COOKIE_SALT,
            max_age=SELLER_COOKIE_AGE))
    except (signing.BadSignature, TypeError, ValueError):
        return set()
