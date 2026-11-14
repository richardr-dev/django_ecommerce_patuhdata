"""Supporting documents kept with a bill, journal, cash transaction or invoice, for the audit."""
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from apps.shops.audit import log as audit_log

from .models import Attachment, Bill, CashTransaction, JournalEntry

TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "pdf": "application/pdf"}
MAX_BYTES = 5 * 1024 * 1024
MAX_PER_RECORD = 20


class AttachmentError(ValueError):
    pass


def _target(store, kind, object_id):
    """The record an attachment belongs to, or None. Always inside ``store``."""
    from apps.tax.models import Invoice
    model = {"bill": Bill, "journal": JournalEntry, "cash": CashTransaction, "invoice": Invoice}.get(kind)
    if model is None:
        return None
    try:
        return model.objects.filter(store=store, pk=object_id).first()
    except (ValueError, TypeError):
        return None


def validate(upload):
    """A PDF or a picture, at most 5 MB, whose content matches its name."""
    if upload is None:
        raise AttachmentError("Pilih file yang akan dilampirkan.")
    if upload.size > MAX_BYTES:
        raise AttachmentError("File terlalu besar (maksimal 5 MB).")
    extension = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
    if extension not in TYPES:
        raise AttachmentError("Unggah PDF atau foto (JPG, PNG, WebP).")
    head = upload.read(8)
    upload.seek(0)
    if extension == "pdf":
        if not head.startswith(b"%PDF"):
            raise AttachmentError("File ini bukan PDF yang valid.")
        return
    from PIL import Image, UnidentifiedImageError
    try:
        Image.open(upload).verify()
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise AttachmentError("File ini bukan foto yang valid.") from None
    finally:
        upload.seek(0)


def add(store, kind, object_id, upload, *, actor, note=""):
    record = _target(store, kind, object_id)
    if record is None:
        raise AttachmentError("Dokumen tidak ditemukan.")
    if Attachment.objects.filter(store=store, kind=kind, object_id=str(object_id)).count() >= MAX_PER_RECORD:
        raise AttachmentError(f"Maksimal {MAX_PER_RECORD} lampiran per dokumen.")
    validate(upload)
    attachment = Attachment.objects.create(store=store, kind=kind, object_id=str(object_id), file=upload,
        name=upload.name[:160], size=upload.size, note=note[:200], uploaded_by=actor)
    audit_log(store, actor, "attachment.added", record, file=attachment.name)
    return attachment


def for_record(store, kind, object_id):
    return list(Attachment.objects.filter(store=store, kind=kind, object_id=str(object_id)
        ).select_related("uploaded_by"))


def response(attachment):
    name = attachment.name
    content_type = TYPES.get(name.rsplit(".", 1)[-1].lower())
    reply = FileResponse(attachment.file.open("rb"), filename=name, as_attachment=content_type is None,
        content_type=content_type or "application/octet-stream")
    reply["Cache-Control"] = "private, no-store"
    reply["X-Content-Type-Options"] = "nosniff"
    return reply


def back(request, default="merchant-dashboard"):
    target = request.POST.get("next", "")
    if target.startswith("/books/") and url_has_allowed_host_and_scheme(target,
            allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(default)


def missing():
    raise Http404
