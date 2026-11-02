"""Code 128 barcodes as inline SVG, for shipping labels. Couriers scan the tracking number in
this symbology; code set B covers every printable ASCII character a tracking number uses."""
from django.utils.html import format_html
from django.utils.safestring import mark_safe

# Bar and space widths (in modules) for symbol values 0-106; 104 is Start B, 106 is Stop.
PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212",
    "221213", "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221",
    "223211", "221132", "221231", "213212", "223112", "312131", "311222", "321122", "321221",
    "312212", "322112", "322211", "212123", "212321", "232121", "111323", "131123", "131321",
    "112313", "132113", "132311", "211313", "231113", "231311", "112133", "112331", "132131",
    "113123", "113321", "133121", "313121", "211331", "231131", "213113", "213311", "213131",
    "311123", "311321", "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111", "111242",
    "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311",
    "113141", "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]
START_B, STOP = 104, 106
QUIET_ZONE = 10  # modules of white on each side, so scanners find the edges


def code128_values(text):
    """Symbol values for `text` in code set B, with start, checksum and stop."""
    if not text or any(not 32 <= ord(character) <= 126 for character in text):
        raise ValueError("Code 128 B encodes printable ASCII only.")
    data = [ord(character) - 32 for character in text]
    checksum = (START_B + sum(position * value for position, value in enumerate(data, 1))) % 103
    return [START_B, *data, checksum, STOP]


def code128_svg(text, *, height=56):
    """An SVG barcode that scales to its container's width; empty for unencodable text."""
    try:
        values = code128_values(text)
    except ValueError:
        return ""
    bars, x = [], QUIET_ZONE
    for value in values:
        for index, width in enumerate(PATTERNS[value]):
            if index % 2 == 0:  # even positions are bars, odd ones spaces
                bars.append(f'<rect x="{x}" width="{width}" height="{height}"/>')
            x += int(width)
    width = x + QUIET_ZONE
    return format_html('<svg class="barcode" viewBox="0 0 {} {}" preserveAspectRatio="none" '
        'role="img" aria-label="Barcode {}" xmlns="http://www.w3.org/2000/svg">'
        '<rect width="100%" height="100%" fill="#fff"/><g fill="#000">{}</g></svg>',
        width, height, text, mark_safe("".join(bars)))
