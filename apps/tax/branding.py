"""How a store's documents look: the logo, accent colour and footer on its invoices.

One place answers it for every document (screen, print, PDF, buyer link, surat tagihan, surat
jalan, quote, credit note): the invoice logo from Tampilan invoice, else the shop's logo, else the
name's initial. A PDF cannot fetch files over the network, so it embeds the logo as a data URI."""
import base64
import mimetypes
from dataclasses import dataclass

from django.core.exceptions import ValidationError

DEFAULT_ACCENT = "#1a66d9"
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_PIXELS = 4000


@dataclass
class Brand:
    name: str
    logo: object  # a FieldFile, or None
    accent: str
    show_name: bool
    footer: str
    address: str = ""

    @property
    def initial(self):
        return (self.name or "?")[:1].upper()

    @property
    def logo_url(self):
        try:
            return self.logo.url if self.logo else ""
        except ValueError:
            return ""

    @property
    def gradient(self):
        return f"linear-gradient(135deg, {self.accent} 0%, {self.accent}cc 100%)"

    def data_uri(self):
        """The logo inlined for the PDF; empty when there is none or it cannot be read."""
        if not self.logo:
            return ""
        try:
            with self.logo.open("rb") as handle:
                raw = handle.read(MAX_LOGO_BYTES + 1)
        except (OSError, ValueError):
            return ""
        if len(raw) > MAX_LOGO_BYTES:
            return ""
        mime = mimetypes.guess_type(self.logo.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def brand(store, invoice=None):
    """The brand for a store. ``invoice`` supplies the name it was issued under."""
    from .models import TaxProfile
    try:
        profile = store.tax_profile
    except TaxProfile.DoesNotExist:  # no tax profile yet
        profile = None
    own = getattr(profile, "invoice_logo", None)
    return Brand(
        name=(getattr(invoice, "seller_name", "") or getattr(profile, "legal_name", "")
            or store.name),
        logo=own if own else (store.logo or None),
        accent=getattr(profile, "invoice_accent", "") or DEFAULT_ACCENT,
        show_name=getattr(profile, "invoice_show_name", True),
        footer=getattr(profile, "invoice_footer", ""),
        address=getattr(profile, "address", ""))


def validate_logo(upload):
    """A logo must be a real, reasonably small picture (the extension alone proves nothing)."""
    from PIL import Image, UnidentifiedImageError
    if upload.size > MAX_LOGO_BYTES:
        raise ValidationError("Ukuran logo maksimal 2 MB.")
    try:
        image = Image.open(upload)
        image.verify()
        width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ValidationError("File ini bukan gambar yang valid. Gunakan PNG, JPG, atau WebP.") from None
    finally:
        upload.seek(0)
    if max(width, height) > MAX_LOGO_PIXELS:
        raise ValidationError(f"Gambar terlalu besar ({width}×{height}px). Maksimal {MAX_LOGO_PIXELS}px.")
