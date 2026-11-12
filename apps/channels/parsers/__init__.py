"""Order-file parsers, one per marketplace export format."""


def parse_order_file(platform, upload):
    """Parse an uploaded order export for ``platform``. Returns ``(orders, skipped)``.

    TikTok Shop and Tokopedia share one export format (Tokopedia sellers moved to the TikTok
    Shop Seller Center), so they share a parser. Platforms without an export parser read the
    Lakuin CSV template."""
    from apps.channels.services import ChannelImportError, parse_orders_csv

    name = getattr(upload, "name", "").lower()
    if platform == "lazada" and name.endswith((".xlsx", ".csv")):
        from .lazada import parse_lazada
        try:
            return parse_lazada(upload)
        except ChannelImportError:
            if not name.endswith(".csv"):
                raise
            upload.seek(0)  # a CSV may be the Lakuin template instead
            return parse_orders_csv(upload), 0
    if not name.endswith(".xlsx"):
        return parse_orders_csv(upload), 0
    if platform == "shopee":
        from .shopee import parse_shopee_xlsx
        return parse_shopee_xlsx(upload)
    if platform in {"tokopedia", "tiktok_shop"}:
        from .tokopedia import parse_tokopedia_xlsx
        return parse_tokopedia_xlsx(upload)
    # Unknown platform: try the formats we know.
    for parser in ("shopee.parse_shopee_xlsx", "tokopedia.parse_tokopedia_xlsx",
            "lazada.parse_lazada"):
        module, function = parser.split(".")
        upload.seek(0)
        try:
            return getattr(__import__(f"apps.channels.parsers.{module}", fromlist=[function]),
                function)(upload)
        except ChannelImportError:
            continue
    raise ChannelImportError("Format file tidak dikenali.")
