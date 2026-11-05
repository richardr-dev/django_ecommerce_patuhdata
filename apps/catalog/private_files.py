"""Files only paying buyers may download (digital products). They never get a public URL:
on disk they sit in MEDIA_ROOT/private, which Caddy refuses to serve (docker/Caddyfile*),
and on S3 they are private objects. Django streams them after checking the order."""
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible
from django.utils.functional import LazyObject


class PrivateStorage(LazyObject):
    def _setup(self):
        if settings.STORAGES["default"]["BACKEND"].endswith("S3Storage"):
            from storages.backends.s3 import S3Storage
            self._wrapped = S3Storage(location="private", default_acl="private",
                querystring_auth=True, custom_domain=None, file_overwrite=False)
        else:
            self._wrapped = FileSystemStorage(location=settings.MEDIA_ROOT / "private",
                base_url=None)


private_storage = PrivateStorage()


def private_storage_callable():
    return private_storage


@deconstructible
class DigitalUploadPath:
    """digital/<store>/<random>/<file name>: the buyer sees the seller's file name."""

    def __call__(self, instance, filename):
        store = getattr(instance, "store", None)
        safe = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or "file"
        return f"digital/{store.slug if store else 'store'}/{uuid.uuid4().hex}/{safe}"
