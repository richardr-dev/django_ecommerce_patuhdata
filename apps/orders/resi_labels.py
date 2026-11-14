"""Read the resi (tracking number) and the marketplace order number from shipping labels in a PDF.

Shopee, Tokopedia and TikTok Shop give labels as PDF with a real text layer, so no OCR engine is needed:
``pypdf`` reads the text. One file may hold many labels, one per page. A photo or a scan has no text layer
and is reported as unreadable instead of guessed at.

What a label does not say in text (the courier is usually a logo) is filled from the order, from the
courier words printed on the label, or, last, guessed from the shape of the resi and shown as a guess."""
import re
from collections import Counter
from dataclasses import dataclass, field

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 300
MARKETPLACE_CHANNELS = ("shopee", "tokopedia", "tiktok_shop", "lazada")
CLOSED_STATUSES = {"pending", "payment_failed", "cancelled", "refunded"}   # no resi for these (as by hand)

COURIERS = {"jne": "JNE", "jnt": "J&T Express", "sicepat": "SiCepat", "anteraja": "AnterAja", "spx": "SPX Express",
    "ninja": "Ninja Xpress", "tiki": "TIKI", "pos": "POS Indonesia", "lion": "Lion Parcel", "wahana": "Wahana",
    "ide": "ID Express", "gosend": "GoSend", "grab": "Grab"}
# Words that name a courier, as printed on a label or in a marketplace's shipping option ("JNE YES").
_WORDS = [("sicepat", "sicepat"), ("anteraja", "anteraja"), ("shopee express", "spx"), ("spx", "spx"),
    ("ninja", "ninja"), ("j&t", "jnt"), ("jnt", "jnt"), ("jne", "jne"), ("tiki", "tiki"),
    ("pos indonesia", "pos"), ("lion parcel", "lion"), ("wahana", "wahana"), ("id express", "ide"),
    ("gosend", "gosend"), ("grab", "grab")]

_LABELED_RESI = re.compile(r"(?i)(?:no\.?\s*resi|nomor\s*resi|\bresi\b|\bawb\b|tracking\s*(?:id|no\.?|number))"
    r"\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9\-]{7,29})")
_BRACKET_REF = re.compile(r"(?i)pesan(?:an)?\s*:?\s*\(([A-Z0-9]{8,30})\)")          # "Pesan: (261107NGDU9QCQ)"
_LABELED_REF = re.compile(r"(?i)(?:no\.?\s*pesanan|nomor\s*pesanan|order\s*(?:id|no\.?|number))\s*[:#.]?\s*"
    r"([A-Z0-9]{8,30})(\s*(?:…|\.\.\.))?")                                              # may be cut short with "…"
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9\-]{7,29}")


class LabelError(ValueError):
    """The file cannot be read as labels; the message is shown to the seller."""


@dataclass
class Label:
    page: int
    readable: bool = True
    resi: str = ""
    refs: list = field(default_factory=list)       # order numbers printed in full, most reliable first
    cut_ref: str = ""                              # an order number cut short by the marketplace ("261107NGDU9Q")
    tokens: set = field(default_factory=set)       # every code-like word on the page, to match a known order
    courier_code: str = ""                         # a courier named in the label's text, if any


@dataclass
class Row:
    index: int
    page: int
    resi: str = ""
    order: object = None
    state: str = "no_order"      # ok, same, differs, closed, no_order, no_resi, no_text, duplicate
    courier_code: str = ""
    courier_source: str = ""     # pesanan, label, tebakan or ""
    note: str = ""

    @property
    def selectable(self):
        return self.state in {"ok", "differs"}


def courier_code_from_text(text):
    """The courier code a free text names ("JNE YES" is jne, "Ninja Xpress" is ninja), or ""."""
    low = (text or "").lower()
    for word, code in _WORDS:
        if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", low):
            return code
    return ""


def guess_courier_from_resi(resi):
    """A guess from the shape of the resi, for a label that names no courier. Only a guess: it is shown as one."""
    value = (resi or "").upper()
    if value.startswith("SPX"):
        return "spx"
    if re.fullmatch(r"00\d{10}", value):
        return "sicepat"
    if re.fullmatch(r"CM\d{9,}", value):
        return "jne"
    if re.fullmatch(r"(JP|JD|JX|JO)\d{9,}", value):
        return "jnt"
    if re.fullmatch(r"SHP\d{8,}", value):
        return "ninja"
    return ""


def _page_label(number, text):
    label = Label(page=number)
    if len(text.strip()) < 10:
        label.readable = False
        return label
    upper = text.upper()
    for match in _BRACKET_REF.finditer(text):
        label.refs.append(match.group(1).upper())
    for match in _LABELED_REF.finditer(text):
        value = match.group(1).upper()
        if match.group(2):
            label.cut_ref = label.cut_ref or value
        elif value not in label.refs:
            label.refs.append(value)
    for match in _LABELED_RESI.finditer(text):
        candidate = match.group(1).upper()
        if re.search(r"\d", candidate):
            label.resi = candidate
            break
    label.tokens = set(_TOKEN.findall(upper))
    if not label.resi:
        # Marketplaces print the resi as a barcode caption again and again; the most repeated code that is
        # not an order number is the resi.
        counts = Counter(token for token in _TOKEN.findall(upper)
            if sum(char.isdigit() for char in token) >= 6 and token not in label.refs)
        repeated = [token for token, n in counts.most_common() if n >= 2]
        label.resi = repeated[0] if repeated else ""
    label.tokens.discard(label.resi)
    label.courier_code = courier_code_from_text(text)
    return label


def read_labels(upload):
    """The labels in an uploaded PDF, one per page. Raises LabelError with a message for the seller."""
    if getattr(upload, "size", 0) > MAX_BYTES:
        raise LabelError("File terlalu besar (maksimal 20 MB).")
    try:
        reader = PdfReader(upload)
        if reader.is_encrypted and not reader.decrypt(""):
            raise LabelError("File PDF terkunci dengan kata sandi.")
        pages = list(reader.pages)
    except LabelError:
        raise
    except (PdfReadError, ValueError, OSError, KeyError, TypeError) as error:
        raise LabelError("File ini bukan PDF yang bisa dibaca.") from error
    if not pages:
        raise LabelError("PDF ini tidak punya halaman.")
    if len(pages) > MAX_PAGES:
        raise LabelError(f"PDF terlalu panjang ({len(pages)} halaman, maksimal {MAX_PAGES}). Bagi menjadi beberapa file.")
    labels = []
    for number, page in enumerate(pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one odd page must not lose the rest
            text = ""
        labels.append(_page_label(number, text))
    return labels


def match_labels(store, labels):
    """Pair every label with an order of the store. Returns ``[Row]`` in page order."""
    from .models import Order
    pool = Order.objects.filter(store=store, channel__in=MARKETPLACE_CHANNELS).exclude(external_order_id="")
    wanted = set()
    for label in labels:
        wanted.update(label.refs)
        wanted.update(label.tokens)
    known = {order.external_order_id.upper(): order for order in
        pool.filter(external_order_id__in=wanted | {w.lower() for w in wanted}).prefetch_related("items")}
    rows, used = [], set()
    for index, label in enumerate(labels):
        row = Row(index=index, page=label.page, resi=label.resi)
        rows.append(row)
        if not label.readable:
            row.state, row.note = "no_text", "Halaman ini tidak berisi teks (foto atau hasil scan)."
            continue
        if not label.resi:
            row.state, row.note = "no_resi", "Nomor resi tidak ditemukan di label ini."
            continue
        order = next((known[ref] for ref in label.refs if ref in known), None)
        if order is None and label.cut_ref and len(label.cut_ref) >= 10:
            cut = list(pool.filter(external_order_id__istartswith=label.cut_ref).prefetch_related("items")[:2])
            order = cut[0] if len(cut) == 1 else None
        if order is None:
            found = {known[token].pk: known[token] for token in label.tokens if token in known}
            order = next(iter(found.values())) if len(found) == 1 else None
        if order is None:
            row.state, row.note = "no_order", "Tidak ada pesanan dengan nomor ini. Impor pesanan marketplace dulu."
            continue
        row.order = order
        if order.pk in used:
            row.state, row.note = "duplicate", "Label kedua untuk pesanan yang sama."
            continue
        used.add(order.pk)
        _resolve_courier(row, label, order)
        if order.status in CLOSED_STATUSES:
            row.state, row.note = "closed", "Pesanan ini batal, dikembalikan, atau belum dibayar."
        elif order.tracking_number and order.tracking_number.upper() == label.resi:
            row.state, row.note = "same", "Resi ini sudah tersimpan."
        elif order.tracking_number:
            row.state, row.note = "differs", f"Pesanan sudah punya resi {order.tracking_number}. Centang untuk menggantinya."
        else:
            row.state = "ok"
    return rows


def _resolve_courier(row, label, order):
    code = courier_code_from_text(order.shipping_service)
    if code:
        row.courier_code, row.courier_source = code, "pesanan"
    elif label.courier_code:
        row.courier_code, row.courier_source = label.courier_code, "label"
    else:
        code = guess_courier_from_resi(label.resi)
        if code:
            row.courier_code, row.courier_source = code, "tebakan"
