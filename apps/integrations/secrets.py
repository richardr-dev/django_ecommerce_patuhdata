import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    material = settings.PAYMENT_SECRETS_KEY or settings.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token):
    """The stored secret, or "" when the key changed and the seller must enter it again."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
