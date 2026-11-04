"""Simple line icons for the back office: {% icon "box" %}. One stroke style in the text
colour, so they look the same on every device (emoji differ per phone and are colourful)."""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# 24×24 outlines, drawn with currentColor strokes.
ICONS = {
    "home": "M3 11 12 4l9 7M5 10v10h14V10",
    "receipt": "M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6",
    "clipboard": "M8 4H6v17h12V4h-2M9 3h6v3H9zM9 12h6M9 16h4",
    "box": "M3 7l9-4 9 4-9 4-9-4zM3 7v10l9 4 9-4V7M12 11v10",
    "users": "M9 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"
        "M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.3c1.8.8 3 2.9 3 5.7",
    "tag": "M3 12V4h8l10 10-8 8zM7.5 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z",
    "layout": "M3 4h18v16H3zM3 9h18M9 9v11",
    "link": "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3"
        "a4 4 0 0 0 5.7 5.7l1-1",
    "wallet": "M3 6h18v14H3zM3 10h18M16 15h2",
    "sliders": "M4 7h9M17 7h3M4 17h3M11 17h9M15 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"
        "M9 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
    "gift": "M3 8h18v4H3zM5 12v8h14v-8M12 8v12M12 8c-1.5-4-6-3.5-5 0M12 8c1.5-4 6-3.5 5 0",
    "alert": "M12 3 2 20h20zM12 10v4M12 17h.01",
    "lock": "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
    "eye": "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "check-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12l3 3 5-6",
    "cash": "M3 6h18v12H3zM12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6.5 9v6M17.5 9v6",
    "trend-down": "M3 7l6 6 4-4 8 8M15 17h6v-6",
    "cart": "M3 4h2l2.5 11h10l2-8H6.5M9 21a1.3 1.3 0 1 0 0-2.6A1.3 1.3 0 0 0 9 21z"
        "M17 21a1.3 1.3 0 1 0 0-2.6 1.3 1.3 0 0 0 0 2.6z",
    "printer": "M7 9V3h10v6M3 9h18v8H3zM7 14h10v7H7z",
    "clock": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    "pin": "M12 21s-7-6.5-7-12a7 7 0 0 1 14 0c0 5.5-7 12-7 12zM12 11.5a2.5 2.5 0 1 0 0-5"
        " 2.5 2.5 0 0 0 0 5z",
    "locate": "M12 19a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM12 2v4M12 18v4M2 12h4M18 12h4",
    "search": "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
    "note": "M4 4h12l4 4v12H4zM8 12h8M8 16h5",
    "calendar": "M3 5h18v16H3zM3 10h18M8 3v4M16 3v4",
    "store": "M4 10v10h16V10M3 4h18l-1 6H4zM10 20v-5h4v5",
    "truck": "M3 6h11v10H3zM14 9h4l3 3v4h-7M7 19.8a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6z"
        "M17 19.8a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6z",
    "plug": "M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0zM12 16v5",
    "star": "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z",
}


@register.simple_tag
def icon(name, label=""):
    path = ICONS.get(name)
    if path is None:
        return ""
    aria = format_html(' role="img" aria-label="{}"', label) if label else mark_safe(
        ' aria-hidden="true"')
    return format_html('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"{}><path d="{}"/></svg>',
        aria, path)
