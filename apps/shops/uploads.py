"""Where uploaded images live: one folder per store, then one per kind of image, e.g.
stores/kopi-kita/products/kopi-250/3f2a….jpg. With S3 the whole tree sits under media/ in the
platform bucket, beside the private backups/ folder. Files get random names, so uploads never
overwrite each other and the original file name (which may hold personal details) is not kept."""
import os
import uuid

from django.utils.deconstruct import deconstructible
from django.utils.text import slugify


def _store_of(instance):
    if hasattr(instance, "slug") and hasattr(instance, "storefront_slides"):
        return instance  # the Store itself
    store = getattr(instance, "store", None)
    if store is None and getattr(instance, "product", None) is not None:
        store = instance.product.store
    return store


def _product_of(instance):
    return instance if hasattr(instance, "sku") and hasattr(instance, "images") else getattr(
        instance, "product", None)


@deconstructible
class StoreUploadPath:
    """upload_to for store images: stores/<store>/<folder>/[<sku>/]<random>.<ext>."""

    def __init__(self, folder):
        self.folder = folder

    def __call__(self, instance, filename):
        store = _store_of(instance)
        parts = ["stores", store.slug if store else "shared", self.folder]
        if self.folder == "products":
            product = _product_of(instance)
            if product is not None and product.sku:
                parts.append(slugify(product.sku)[:60] or "produk")
        extension = os.path.splitext(filename)[1].lower()[:10] or ".jpg"
        return "/".join(parts) + f"/{uuid.uuid4().hex}{extension}"

    def __eq__(self, other):
        return isinstance(other, StoreUploadPath) and other.folder == self.folder
