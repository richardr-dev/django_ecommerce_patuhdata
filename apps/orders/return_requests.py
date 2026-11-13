"""A buyer asks to return or exchange something they received.

The buyer picks the item, says whether they want their money back or a different size, gives the
reason and a photo of the problem, and (for an exchange) picks a size that is in stock. The seller
then approves, receives, inspects and settles it (apps/tax/returns.py). The photo is required so
the seller can judge the claim before the parcel travels."""
import logging

from django.core.exceptions import ValidationError

from .models import OrderItem, ReturnRequest

logger = logging.getLogger(__name__)

RETURNABLE_STATUSES = {"shipped", "delivered", "completed"}
OPEN_STATUSES = {"requested", "approved", "item_received", "inspected", "replacement_sent"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 6000


class ReturnRequestError(ValueError):
    pass


def validate_photo(upload):
    """A real picture (PNG, JPG, WebP), at most 5 MB. The file name proves nothing, so it is opened."""
    from PIL import Image, UnidentifiedImageError
    if upload is None:
        raise ReturnRequestError("Lampirkan foto barang agar penjual bisa memeriksa pengajuan Anda.")
    if upload.size > MAX_PHOTO_BYTES:
        raise ReturnRequestError("Ukuran foto maksimal 5 MB.")
    try:
        image = Image.open(upload)
        image.verify()
        fmt, size = image.format, image.size
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ReturnRequestError("File itu bukan foto yang valid. Gunakan PNG, JPG, atau WebP.") from None
    finally:
        upload.seek(0)
    if fmt not in {"PNG", "JPEG", "WEBP"}:
        raise ReturnRequestError("Foto harus berformat PNG, JPG, atau WebP.")
    if max(size) > MAX_PHOTO_PIXELS:
        raise ReturnRequestError("Foto terlalu besar. Kecilkan resolusinya lalu coba lagi.")


def available_sizes(order_item):
    """Other sizes/variants of the same product that are in stock right now."""
    if not order_item.product_id:
        return []
    return [v for v in order_item.product.variants.filter(is_active=True).order_by("name")
        if v.pk != order_item.variant_id and v.stock - v.reserved > 0]


def open_request(order_item):
    return ReturnRequest.objects.filter(order_item=order_item, status__in=OPEN_STATUSES).first()


def create_request(order, *, order_item_id, request_type, reason, reason_detail="", quantity=1,
        photo=None, replacement_variant_id=None):
    """Raises ReturnRequestError with a message the buyer can read."""
    if order.status not in RETURNABLE_STATUSES:
        raise ReturnRequestError("Pengajuan baru bisa dibuat setelah pesanan dikirim.")
    item = OrderItem.objects.filter(order=order, pk=order_item_id).select_related("product",
        "variant").first()
    if item is None:
        raise ReturnRequestError("Pilih barang yang ingin dikembalikan atau ditukar.")
    if request_type not in dict(ReturnRequest.TYPES):
        raise ReturnRequestError("Pilih retur atau tukar ukuran.")
    if reason not in dict(ReturnRequest.REASONS):
        raise ReturnRequestError("Pilih alasan pengajuan.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 0
    if not 1 <= quantity <= item.quantity:
        raise ReturnRequestError(f"Jumlah harus antara 1 dan {item.quantity}.")
    if open_request(item):
        raise ReturnRequestError("Barang ini sudah punya pengajuan yang sedang diproses.")
    validate_photo(photo)
    replacement = None
    if request_type == "exchange":
        replacement = next((v for v in available_sizes(item) if str(v.pk) == str(replacement_variant_id)), None)
        if replacement is None:
            raise ReturnRequestError("Pilih ukuran pengganti yang masih tersedia.")
        if replacement.stock - replacement.reserved < quantity:
            raise ReturnRequestError(f"Ukuran {replacement.name} hanya tersisa "
                f"{replacement.stock - replacement.reserved}.")
    try:
        return ReturnRequest.objects.create(order=order, order_item=item, quantity=quantity,
            request_type=request_type, reason=reason, reason_detail=reason_detail.strip()[:1000],
            original_variant=item.variant, replacement_variant=replacement, photo=photo)
    except ValidationError as error:  # pragma: no cover - defensive
        raise ReturnRequestError("; ".join(error.messages)) from error
