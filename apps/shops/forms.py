import re

from django import forms

SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])")
INSTAGRAM_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}")


def clean_instagram_handle(value):
    """Accept "@toko", "toko" or a profile URL; store the bare username."""
    handle = (value or "").strip()
    for prefix in ("https://", "http://", "www.", "instagram.com/", "@"):
        handle = handle.removeprefix(prefix)
    handle = handle.split("?")[0].strip("/")
    if handle and not INSTAGRAM_PATTERN.fullmatch(handle):
        raise forms.ValidationError("Masukkan nama pengguna Instagram, misalnya batiksari.id.")
    return handle
